"""选中文字后弹出的朗读面板：不用记快捷键，松手就有，点一下就念。

为什么是「Canvas + Tk 原生文字」而不是「Pillow 画好整张图」
----------------------------------------------------------
整张图用 Pillow 画会把 13px 的中文糊成一团（没有次像素抗锯齿）。文字交给
Tk 自己渲染：Canvas 上先用 Pillow 铺一张圆角底图（只有色块，没有字），再用
`create_text` 写文字 —— 笔画是 GDI 出的，清晰、粗细正常，改字号也只改一行。

面板结构（三行：主操作 + 自定义语速 + 音色下拉）
--------------------------------------
    ┌──────────────────────┬────────┐
    │        朗读          │  暂停  │      实心主按钮（蓝/灰蓝），白字
    ├──────┬──────┬────────┼────────┤
    │      自定义语速：1.13×         │      点一下原地变输入框，回车即换挡
    ├───────────────────────────────┤
    │  晓晓 · 女 温柔           ▾  │      点开在浮窗内展开音色列表
    └───────────────────────────────┘
格子之间留 4px 露出面板底色，形成细网格；点击/悬停都按格子矩形自己算坐标，
不用 Canvas 的 item 绑定（重绘时 item 会全被删掉）。

三条必须守住的交互底线
----------------------
1. **浮窗不能抢焦点**。一旦抢焦点，很多应用会立刻清掉选区高亮，用户会觉得
   「我的选中被取消了」，后续 Ctrl+C 也拿不到东西。窗口加了 `WS_EX_NOACTIVATE`，
   并且只 `deiconify` + `lift`，从不 `focus_force`。
2. **点击浮窗不能先把它自己关掉**。全局钩子比应用的鼠标消息先到，钩子那边收到
   左键按下时要先问 `contains(x, y)`，落在浮窗上就放行。
3. **不能挡住选区**。默认出现在鼠标上方 16px，上方没空间才翻到下方。

线程模型
--------
tkinter 不是线程安全的，Tk 独占一个线程，外部只往队列丢指令
（show / hide / refresh / quit），由 Tk 线程的 `after` 轮询取走执行。
朗读、切倍速要发 HTTP 请求，一律丢到后台线程，绝不阻塞 Tk 线程。
"""
import ctypes
import logging
import queue
import threading
import time

log = logging.getLogger('tts.bubble')

TRANSPARENT = '#FF00FF'      # 抠色用，别用在界面配色上
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# ---- 面板几何（96dpi 下的像素，实际按 DPI 系数等比放大）----
PAD = 6
ROW1_H = 30
ROW2_H = 26
ROW3_H = 26          # 音色行 / 展开后的每个音色项
GAP = 4
W = 196
H_BASE = PAD * 2 + ROW1_H + GAP + ROW2_H + GAP + ROW3_H   # 102（收起时）
H_MAX_MARGIN = 8 * (ROW3_H + GAP)   # 最多再展开 8 项也放得下

# ---- 配色：浅色底 + 白格子 + 两个实心主按钮 ----
C_BG = (246, 248, 251)
C_EDGE = (202, 208, 218)
C_CELL = (255, 255, 255)
C_CELL_EDGE = (214, 220, 230)
C_CELL_HOVER = (233, 238, 246)
C_CELL_HOVER_EDGE = (150, 162, 182)
C_PRIMARY = (44, 105, 232)
C_PRIMARY_HOVER = (61, 122, 245)
C_TOGGLE = (104, 118, 138)          # 暂停：灰蓝实心，白字（次级主按钮）
C_TOGGLE_HOVER = (83, 96, 114)
C_ON = (222, 238, 255)
C_ON_EDGE = (96, 158, 246)
C_TEXT = '#ffffff'                  # 朗读/暂停：实心底上的白字
C_TEXT_DIM = '#5d6774'
C_TEXT_ON = '#1a5fd0'

FONT_FAMILY = 'Microsoft YaHei UI'


def _no_activate(hwnd):
    """给窗口加 WS_EX_NOACTIVATE：点击时不抢焦点，原窗口的选区不会消失。"""
    try:
        user32 = ctypes.windll.user32
        get = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
        set_ = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
        cur = get(hwnd, GWL_EXSTYLE)
        set_(hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        return True
    except Exception as e:
        log.warning('设置 WS_EX_NOACTIVATE 失败（浮窗可能会抢焦点）: %s', e)
        return False


def _dpi_factor():
    """物理像素 ÷ tkinter 坐标的换算系数。

    钩子拿到的是物理像素，而 tkinter 在非 DPI-aware 进程里用的是被系统缩放过的
    逻辑坐标。不换算的话，150% 缩放下浮窗会歪一半的距离。
    """
    try:
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForSystem()          # Win10 1703+
        f = dpi / 96.0
        return f if 0.5 <= f <= 4.0 else 1.0
    except Exception:
        return 1.0


def _rate_text(r):
    """1.0 → '1.0×'，0.75 → '0.75×'，1.5 → '1.5×'（不出现 1.50× 这种）。"""
    s = f'{float(r):.2f}'.rstrip('0')
    if s.endswith('.'):
        s += '0'
    return f'{s}×'


class Bubble:
    def __init__(self, on_click, hide_ms=4000, on_toggle=None, on_rate=None,
                 on_voice=None, get_state=None,
                 rate_min=0.5, rate_max=3.0):
        self.on_click = on_click            # 点了「朗读」：回调(text)
        self.on_toggle = on_toggle          # 点了「暂停/继续」：回调()
        self.on_rate = on_rate              # 自定义语速确认：回调(rate)
        self.on_voice = on_voice            # 选了某个音色：回调(voice_key)
        self.get_state = get_state          # 取 {'rate':..,'paused':..,'voice':..,
                                            #     'voices':[(key,label)..]}，纯本地读
        self.rate_min = float(rate_min)
        self.rate_max = float(rate_max)
        self.hide_ms = hide_ms

        self._q = queue.Queue()
        self._ready = threading.Event()
        self._thread = None
        self._root = None
        self._cv = None
        self._alive = False
        self._f = _dpi_factor()

        self._rect = None       # (x, y, w, h) 物理像素；None = 当前没显示
        self._text = ''
        self._state = {'rate': None, 'paused': False, 'voice': None,
                       'voices': []}
        self._voice_open = False    # 音色下拉是否展开
        self._hover = None
        self._photo = None
        self._seq = 0
        self._rate_entry = None      # 「自定义语速」原地输入框（None = 没在编辑）
        self._rate_rect = None       # 编辑中那格的矩形，_redraw 重摆输入框要用
        # 被点过的时刻。点浮窗之后紧接着会来一次左键松开（钩子看到的），
        # 如果不去重就会「朗读的同时又弹一个新浮窗」，所以外层要拿它做判断。
        self.last_click = 0.0

        self._buttons = self._layout()

    # ---------------------------- 布局 ----------------------------

    def _layout(self):
        """[(key, rect)]，rect 是 96dpi 基准坐标 (x0, y0, x1, y1)。

        第三行是音色下拉：收起时只有一个「当前音色 ▾」格子，展开后在其下
        面追加每个音色一行，面板高度（self.H_cur）随之变化。
        """
        out = [('speak', (PAD, PAD, PAD + 128, PAD + ROW1_H)),
               ('toggle', (PAD + 128 + GAP, PAD, W - PAD, PAD + ROW1_H))]
        y0 = PAD + ROW1_H + GAP
        # 倍速行不摆预设档位：整行一个「自定义语速」格，点了开独立输入框
        out.append(('rate:custom', (PAD, y0, W - PAD, y0 + ROW2_H)))
        yv = y0 + ROW2_H + GAP
        out.append(('voice', (PAD, yv, W - PAD, yv + ROW3_H)))
        y = yv + ROW3_H + GAP
        if self._voice_open:
            for key, _label in self._voices():
                out.append((f'voice:{key}', (PAD, y, W - PAD, y + ROW3_H)))
                y += ROW3_H + GAP
        self.H_cur = y + PAD - GAP if self._voice_open else H_BASE
        return out

    # ---------------------------- 外部接口 ----------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name='tts-bubble',
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(4.0):
            log.error('浮窗 4 秒内没起来（tkinter 初始化卡住？）')
        return self

    def stop(self):
        if self._alive:
            self._q.put(('quit',))
            t = self._thread
            if t is not None and t.is_alive():
                t.join(1.5)
        self._thread = None

    def show(self, text, x, y):
        """在物理坐标 (x, y)（鼠标位置）附近显示浮窗。"""
        if self._alive:
            self._q.put(('show', text, int(x), int(y)))

    def hide(self):
        if self._alive:
            self._q.put(('hide',))

    def call(self, fn, *args):
        """让 Tk 线程执行 fn(*args)。只给测试/调试用（Tk 不是线程安全的）。"""
        if self._alive:
            self._q.put(('call', fn, args))

    def contains(self, x, y):
        """(x, y) 是否落在浮窗上。全局钩子用它决定「这一下点击要不要先隐藏浮窗」。"""
        r = self._rect
        if not r:
            return False
        return r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]

    @property
    def visible(self):
        return self._rect is not None

    def size(self):
        """(宽, 高) 物理像素。高度随音色下拉展开与否变化。"""
        return int(round(W * self._f)), int(round(getattr(self, 'H_cur', H_BASE) * self._f))

    # ---------------------------- Tk 线程 ----------------------------

    def _run(self):
        try:
            import tkinter as tk
            import tkinter.font as tkfont
        except Exception as e:
            log.error('没有 tkinter，浮窗不可用: %s', e)
            self._ready.set()
            return
        try:
            root = tk.Tk()
        except Exception as e:
            log.error('创建 Tk 窗口失败: %s', e)
            self._ready.set()
            return

        root.overrideredirect(True)
        root.attributes('-topmost', True)
        try:
            root.attributes('-transparentcolor', TRANSPARENT)
        except Exception:
            pass
        root.configure(bg=TRANSPARENT)

        w, h = self.size()
        cv = tk.Canvas(root, width=w, height=h, bg=TRANSPARENT,
                       highlightthickness=0, bd=0)
        cv.pack()

        self._tk = tk
        self._root = root
        self._cv = cv

        fams = set(tkfont.families(root))
        fam = FONT_FAMILY if FONT_FAMILY in fams else (
            'Microsoft YaHei' if 'Microsoft YaHei' in fams else 'TkDefaultFont')
        f = self._f

        def font(px, bold=False):
            return tkfont.Font(root=root, family=fam, size=-int(round(px * f)),
                               weight='bold' if bold else 'normal')

        self._fonts = {'speak': font(13, True), 'toggle': font(12, True),
                       'rate': font(11)}

        cv.bind('<Button-1>', self._on_press)
        cv.bind('<Motion>', self._on_motion)
        cv.bind('<Leave>', self._on_leave)

        root.withdraw()
        root.update_idletasks()
        _no_activate(root.winfo_id())

        self._alive = True
        self._ready.set()
        root.after(30, self._poll)
        try:
            root.mainloop()
        finally:
            self._alive = False
            log.debug('浮窗线程结束')

    # ---------------------------- 绘制 ----------------------------

    def _rate(self):
        r = self._state.get('rate')
        return None if r is None else float(r)

    def _rate_picked(self, key):
        """某个倍速格是否该高亮。现在倍速行只有自定义一格，恒不亮
        （它是个入口不是档位）。保留函数是为了绘制逻辑对任意 key 都安全。"""
        if not key.startswith('rate:'):
            return False
        if key == 'rate:custom':
            return False
        try:
            return (self._rate() is not None
                    and abs(float(key[5:]) - self._rate()) < 1e-6)
        except ValueError:
            return False

    def _voices(self):
        return self._state.get('voices') or []

    def _voice_label(self, key):
        for k, label in self._voices():
            if k == key:
                return label
        return None

    def _label(self, key):
        if key == 'speak':
            return '朗读'
        if key == 'toggle':
            return '继续' if self._state.get('paused') else '暂停'
        if key == 'voice':
            cur = self._voice_label(self._state.get('voice'))
            arrow = '▴' if self._voice_open else '▾'
            return f'{cur or "音色"}  {arrow}'
        if key.startswith('voice:'):
            return self._voice_label(key[6:]) or key[6:]
        if key == 'rate:custom':
            cur = self._rate()
            return (f'语速：{_rate_text(cur)}' if cur is not None
                    else '语速')
        return _rate_text(key[5:])

    def _redraw(self):
        if self._root is None or self._cv is None:
            return
        self._buttons = self._layout()      # 布局依赖展开状态/音色列表，每次重算
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception as e:
            log.error('没有 Pillow，浮窗画不出来: %s', e)
            return

        f = self._f
        wi, hi = self.size()
        # Canvas 尺寸必须跟着面板高度走：它只在浮窗线程启动时创建一次，
        # 展开音色列表后如果不重设，多出来的行会被整个裁掉（点了没反应）。
        try:
            self._cv.configure(width=wi, height=hi)
        except Exception:
            pass
        img = Image.new('RGB', (wi, hi), (255, 0, 255))   # 洋红 = 抠掉的区域
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, wi - 1, hi - 1], radius=max(4, int(8 * f)),
                            fill=C_BG, outline=C_EDGE)

        cur = self._rate()
        for key, r in self._buttons:
            box = [r[0] * f, r[1] * f, r[2] * f - 1, r[3] * f - 1]
            radius = max(3, int(5 * f))
            if key == 'speak':
                fill = C_PRIMARY_HOVER if self._hover == key else C_PRIMARY
                d.rounded_rectangle(box, radius=radius, fill=fill)
                continue
            if key == 'toggle':
                fill = C_TOGGLE_HOVER if self._hover == key else C_TOGGLE
                d.rounded_rectangle(box, radius=radius, fill=fill)
                continue
            picked = (self._rate_picked(key)
                      or (key.startswith('voice:')
                          and key[6:] == self._state.get('voice')))
            if picked:
                d.rounded_rectangle(box, radius=radius, fill=C_ON,
                                    outline=C_ON_EDGE, width=max(1, int(f)))
            else:
                hovered = self._hover == key
                d.rounded_rectangle(
                    box, radius=radius,
                    fill=C_CELL_HOVER if hovered else C_CELL,
                    outline=C_CELL_HOVER_EDGE if hovered else C_CELL_EDGE,
                    width=max(1, int(f)))

        self._photo = ImageTk.PhotoImage(img)
        cv = self._cv
        cv.delete('all')
        cv.create_image(0, 0, image=self._photo, anchor='nw')

        cur = self._rate()
        for key, r in self._buttons:
            cx = (r[0] + r[2]) / 2 * f
            cy = (r[1] + r[3]) / 2 * f
            if key == 'speak':
                color = C_TEXT
                fnt = self._fonts['speak']
            elif key == 'toggle':
                color = C_TEXT
                fnt = self._fonts['toggle']
            elif key.startswith('voice'):
                picked = (key == 'voice:{}'.format(self._state.get('voice')))
                color = C_TEXT_ON if picked else C_TEXT_DIM
                fnt = self._fonts['rate']
            else:
                picked = self._rate_picked(key)
                color = C_TEXT_ON if picked else C_TEXT_DIM
                fnt = self._fonts['rate']
            cv.create_text(cx, cy, text=self._label(key), font=fnt,
                           fill=color, anchor='center')

        # 编辑中的输入框要重新摆回去（上面 delete('all') 把它的窗口 item 删了）
        if self._rate_entry is not None:
            for key, r in self._buttons:
                if key == 'rate:custom':
                    self._rate_rect = r
                    break
            self._place_rate_entry()

    # ---------------------------- 指令循环 ----------------------------

    def _poll(self):
        try:
            while True:
                cmd = self._q.get_nowait()
                if cmd[0] == 'show':
                    self._do_show(*cmd[1:])
                elif cmd[0] == 'hide':
                    self._do_hide()
                elif cmd[0] == 'refresh':
                    self._state = self._read_state()
                    self._redraw()
                elif cmd[0] == 'call':
                    try:
                        cmd[1](*cmd[2])
                    except Exception:
                        log.error('浮窗 call 出错', exc_info=True)
                elif cmd[0] == 'quit':
                    self._do_hide()
                    self._root.quit()
                    return
        except queue.Empty:
            pass
        except Exception:
            log.error('浮窗指令处理出错', exc_info=True)
        try:
            self._root.after(30, self._poll)
        except Exception:
            pass

    def _read_state(self):
        st = {'rate': None, 'paused': False, 'voice': None, 'voices': []}
        if self.get_state is not None:
            try:
                s = self.get_state() or {}
                st['rate'] = s.get('rate')
                st['paused'] = bool(s.get('paused'))
                st['voice'] = s.get('voice')
                st['voices'] = list(s.get('voices') or [])
            except Exception:
                pass
        return st

    def _do_show(self, text, x, y):
        self._seq += 1
        self._text = text
        self._voice_open = False            # 每次弹出都收起音色列表
        self._state = self._read_state()
        self._hover = None
        self._redraw()

        f = self._f
        w, h = self.size()
        sw = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
        sh = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)

        px = max(4, min(x - w // 2, sw - w - 4))
        py = y - h - 16                 # 默认放在鼠标上方
        if py < 4:                      # 上方没地方就翻到下方
            py = min(y + 18, sh - h - 4)

        self._root.geometry(f'{w}x{h}+{int(px / f)}+{int(py / f)}')
        self._root.deiconify()
        self._root.lift()               # lift 不激活（有 WS_EX_NOACTIVATE）
        self._rect = (px, py, w, h)
        self._timer()

    def _apply_geom(self):
        """展开/收起音色列表后重设窗口尺寸（保持左上角不动，向下生长）。"""
        if self._rect is None or self._root is None:
            return
        f = self._f
        px, py, _w, _h = self._rect
        w, h = self.size()
        sh = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)
        if py + h > sh - 4:                 # 往下长不下就整体上移
            py = max(4, sh - 4 - h)
        self._root.geometry(f'{w}x{h}+{int(px / f)}+{int(py / f)}')
        self._rect = (px, py, w, h)

    def _timer(self, ms=None):
        seq = self._seq
        self._root.after(self.hide_ms if ms is None else ms,
                         lambda: self._timeout(seq))

    def _timeout(self, seq):
        if seq == self._seq and self._rect is not None:
            self._do_hide()

    def _do_hide(self):
        if self._root is None:
            return
        self._close_rate_input()        # 连带恢复 NOACTIVATE，浮窗回归不抢焦点
        try:
            self._root.withdraw()
        except Exception:
            pass
        self._rect = None
        self._hover = None

    # ---------------------------- 鼠标 ----------------------------

    def _hit(self, x, y):
        """(x, y) 是 96dpi 基准坐标，返回命中的格子 key。"""
        for key, r in self._buttons:
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return key
        return None

    def _on_motion(self, ev):
        key = self._hit(ev.x / self._f, ev.y / self._f)
        if key == self._hover:
            return
        self._hover = key
        try:
            self._cv.configure(cursor='hand2' if key else 'arrow')
        except Exception:
            pass
        self._redraw()

    def _on_leave(self, _ev=None):
        if self._hover is None:
            return
        self._hover = None
        self._redraw()

    def _on_press(self, ev):
        key = self._hit(ev.x / self._f, ev.y / self._f)
        if not key:
            return
        self.last_click = time.time()
        self._seq += 1                  # 作废原来的自动消失计时

        if key == 'speak':
            text = self._text
            self._do_hide()
            if text and self.on_click is not None:
                self._bg(self._fire, text)
            return

        if key == 'toggle':
            if self.on_toggle is None:
                self._do_hide()
                return
            self._bg(self._fire_toggle)
            self._timer()               # 留在原地，让用户接着点倍速/朗读
            return

        if key == 'rate:custom':
            self._open_rate_input()
            return

        if key.startswith('rate:'):
            r = float(key[5:])
            if self._rate() is not None and abs(self._rate() - r) < 1e-6:
                self._timer()
                return
            old = self._state.get('rate')
            self._state['rate'] = r     # 乐观更新：先亮起来，失败再退回去
            self._redraw()
            if self.on_rate is not None:
                self._bg(self._fire_rate, r, old)
            self._timer()
            return

        if key == 'voice':
            if not self._voices():      # 没有可选音色（状态没拿到）就不动
                self._timer()
                return
            self._voice_open = not self._voice_open
            self._redraw()
            self._apply_geom()
            self._timer()
            return

        if key.startswith('voice:'):
            v = key[6:]
            if v == self._state.get('voice'):
                self._voice_open = False
                self._redraw()
                self._apply_geom()
                self._timer()
                return
            old = self._state.get('voice')
            self._state['voice'] = v    # 乐观更新，失败退回
            self._voice_open = False
            self._redraw()
            self._apply_geom()
            if self.on_voice is not None:
                self._bg(self._fire_voice, v, old)
            self._timer()
            return

    # ---------------------------- 自定义语速 ----------------------------

    def _open_rate_input(self):
        """把「自定义语速」那格原地变成输入框：不弹独立窗口，直接在浮窗上输。

        浮窗本体带 WS_EX_NOACTIVATE 收不到键盘，编辑期间临时把这个样式摘掉、
        结束（回车/Esc/点别处）再挂回去。激活浮窗可能让源应用清掉选区高亮，
        但要读的字在弹出那一刻已经抓进 self._text 了，不影响朗读。
        只能在 Tk 线程里调（_on_press / call 队列都是）。
        """
        if self._rate_entry is not None or self._root is None or self._cv is None:
            return
        rect = None
        for key, r in self._buttons:
            if key == 'rate:custom':
                rect = r
                break
        if rect is None:
            return
        self._seq += 1                  # 编辑期间不自动消失
        self._rate_rect = rect
        self._allow_focus(True)
        cur = self._rate()
        init = ('%.2f' % cur).rstrip('0').rstrip('.') if cur is not None else '1.0'
        fam = self._fonts['rate'].actual('family')
        ent = self._tk.Entry(
            self._cv, justify='center',
            font=(fam, -int(round(12 * self._f))),
            bg='#ffffff', fg='#1a5fd0', insertbackground='#1a5fd0',
            highlightthickness=1, highlightbackground='#96a2b6',
            highlightcolor='#2c69e8')
        self._rate_entry = ent
        self._place_rate_entry()
        ent.insert(0, init)
        ent.bind('<Return>', self._apply_rate_input)
        ent.bind('<Escape>', self._cancel_rate_input)
        ent.bind('<FocusOut>', self._cancel_rate_input)
        self._root.focus_force()
        ent.focus_set()
        ent.select_range(0, 'end')

    def _place_rate_entry(self):
        """把输入框摆到「自定义语速」格的矩形上。_redraw 会删光画布 item，
        所以每次重绘后都要重新摆一次。"""
        if self._rate_entry is None or self._rate_rect is None or self._cv is None:
            return
        f = self._f
        x0, y0, x1, y1 = [int(round(v * f)) for v in self._rate_rect]
        self._cv.create_window((x0 + x1) // 2, (y0 + y1) // 2,
                               window=self._rate_entry,
                               width=x1 - x0 - 2, height=y1 - y0 - 2)

    def _allow_focus(self, on):
        """摘/挂 WS_EX_NOACTIVATE：输入期间要抢键盘，平时绝不能抢焦点。"""
        try:
            user32 = ctypes.windll.user32
            get = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
            set_ = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
            hwnd = self._root.winfo_id()
            cur = get(hwnd, GWL_EXSTYLE)
            if on:
                set_(hwnd, GWL_EXSTYLE, cur & ~WS_EX_NOACTIVATE)
            else:
                set_(hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            return True
        except Exception as e:
            log.warning('切换浮窗可聚焦状态失败: %s', e)
            return False

    def _close_rate_input(self):
        """收掉输入框、恢复 NOACTIVATE。幂等，随便调。"""
        ent, self._rate_entry = self._rate_entry, None
        self._rate_rect = None
        if self._root is not None:
            self._allow_focus(False)
        if ent is not None:
            try:
                ent.destroy()
            except Exception:
                pass

    def _apply_rate_input(self, _ev=None):
        ent = self._rate_entry
        if ent is None:
            return
        s = ent.get().strip().rstrip('xX×')     # 容忍 1.25x / 1.25×
        self._close_rate_input()
        try:
            r = float(s)
        except ValueError:
            self._timer()               # 输错当取消，浮窗恢复原样
            return
        if not (self.rate_min <= r <= self.rate_max):
            self._timer()
            return
        cur2 = self._rate()
        if cur2 is not None and abs(cur2 - r) < 1e-6:
            self._timer()
            return
        old = self._state.get('rate')
        self._state['rate'] = r         # 乐观更新：先亮出来，失败再退回去
        self._redraw()
        if self.on_rate is not None:
            self._bg(self._fire_rate, r, old)
        self._timer()

    def _cancel_rate_input(self, _ev=None):
        if self._rate_entry is None:
            return
        self._close_rate_input()
        self._timer()

    # ---------------------------- 后台动作 ----------------------------

    def _bg(self, fn, *args):
        """朗读/切倍速都要发 HTTP，不能在 Tk 线程里做。"""
        threading.Thread(target=fn, args=args, daemon=True,
                         name='tts-bubble-act').start()

    def _fire(self, text):
        try:
            self.on_click(text)
        except Exception as e:
            log.error('浮窗点击回调失败: %s', e, exc_info=True)

    def _fire_toggle(self):
        try:
            self.on_toggle()
        except Exception as e:
            log.error('浮窗暂停回调失败: %s', e, exc_info=True)
        finally:
            # 等 player 状态落定再刷新，按钮才会从「暂停」变成「继续」
            time.sleep(0.12)
            self._q.put(('refresh',))

    def _fire_rate(self, rate, old):
        try:
            r = self.on_rate(rate)
            if isinstance(r, dict) and r.get('ok') is False:
                raise RuntimeError(r.get('reason') or '切换失败')
        except Exception as e:
            log.warning('浮窗切倍速失败（%.2f×）: %s', rate, e)
            self._state['rate'] = old
            self._q.put(('refresh',))
        else:
            self._q.put(('refresh',))

    def _fire_voice(self, voice, old):
        try:
            r = self.on_voice(voice)
            if isinstance(r, dict) and r.get('ok') is False:
                raise RuntimeError(r.get('reason') or '切换失败')
        except Exception as e:
            log.warning('浮窗切音色失败（%s）: %s', voice, e)
            self._state['voice'] = old
            self._q.put(('refresh',))
        else:
            self._q.put(('refresh',))
