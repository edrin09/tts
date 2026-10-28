"""语音输入：按住 Alt 说话，松开自动识别并打进光标处。

状态机（故意只做四态）
----------------------
    idle --Alt按下--> pending(宽限) --250ms内无其它键--> recording
                      |                                    |
                      +--组合键出现→idle                   +--Alt松开/超时--> finishing → idle
                                                           +--组合键出现→ 取消 → idle

为什么 Alt 能当 PTT 键
----------------------
Alt+右键/Alt+中键/Alt+滚轮这些既有组合键都是在「Alt 按下之后 250ms 内」出现
鼠标键的，宽限期内只要见到任何其它键（键盘或鼠标）就取消 PTT，所以两条链路
互不抢。单按 Alt 本身不吞（系统默认行为不动），代价是有的应用会把单击 Alt
当菜单键，可接受。

边说边出字
----------
录音回调只往队列丢样本；识别线程逐块喂 recognizer 并把 partial 推给小窗。
实测 RTF ≈ 0.11（5.6s 音频 0.62s 解完），2 线程下识别永远不会追上话筒。

小窗不抢焦点：WS_EX_NOACTIVATE（同 bubble.py 的做法），打字目标不会被挤掉。
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import time

import numpy as np

from core import config

log = logging.getLogger('tts.dictate')

VK_MENU = 0x12
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20

# 小窗样式（96dpi 基准，实际按 DPI 系数放大）
WIN_W = 300
WIN_H = 56
BG = '#2b3038'
FG = '#e8ecf1'
FG_DIM = '#9aa3ad'
ACCENT = '#7db4ff'
FONT_FAMILY = 'Microsoft YaHei UI'

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


def _no_activate(hwnd):
    try:
        user32 = ctypes.windll.user32
        get = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
        set_ = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
        set_(hwnd, GWL_EXSTYLE,
             get(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception as e:
        log.debug('WS_EX_NOACTIVATE 设置失败: %s', e)


def _dpi_factor():
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        f = dpi / 96.0
        return f if 0.5 <= f <= 4.0 else 1.0
    except Exception:
        return 1.0


def _cursor_pos():
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def type_text(text):
    """把文字打进当前焦点窗口（SendInput UNICODE，不碰剪贴板）。"""
    if not text:
        return 0
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                    ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(wt.DWORD))]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD),
                    ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                    ('dwExtraInfo', ctypes.POINTER(wt.DWORD))]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [('uMsg', wt.DWORD), ('wParamL', wt.WORD), ('wParamH', wt.WORD)]

    class _INPUT(ctypes.Union):
        _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [('type', wt.DWORD), ('union', _INPUT)]

    def _send(ch, up=False):
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        inp = INPUT(INPUT_KEYBOARD, _INPUT(ki=KEYBDINPUT(0, ord(ch), flags, 0, None)))
        if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)):
            raise OSError(kernel32.GetLastError())

    n = 0
    for ch in text:
        if ch in '\r\n\t':
            ch = ' '
        if ord(ch) > 0xFFFF:        # 代理对：高低半字各发一次
            for half in ch:
                _send(half)
                _send(half, up=True)
                n += 1
            continue
        _send(ch)
        _send(ch, up=True)
        n += 1
    return n


class ListenWindow:
    """「正在听 + partial 文字」小窗。Tk 独占线程，外部只丢指令。"""

    def __init__(self):
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._thread = None
        self._alive = False
        self._f = _dpi_factor()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name='tts-listen',
                                        daemon=True)
        self._thread.start()
        self._ready.wait(4.0)
        return self

    def stop(self):
        if self._alive:
            self._q.put(('quit',))
            if self._thread is not None:
                self._thread.join(1.5)
        self._thread = None

    def show(self, x, y, text=''):
        if self._alive:
            self._q.put(('show', int(x), int(y), text))

    def update(self, text):
        if self._alive:
            self._q.put(('text', text))

    def hide(self):
        if self._alive:
            self._q.put(('hide',))

    # ---------------------------- Tk 线程 ----------------------------

    def _run(self):
        try:
            import tkinter as tk
            import tkinter.font as tkfont
        except Exception as e:
            log.error('没有 tkinter，听写小窗不可用: %s', e)
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
        root.configure(bg=BG)
        f = self._f
        root.geometry(f'{int(WIN_W * f)}x{int(WIN_H * f)}+0+0')

        fams = set(tkfont.families(root))
        fam = FONT_FAMILY if FONT_FAMILY in fams else 'Microsoft YaHei'
        self._font = tkfont.Font(root=root, family=fam,
                                 size=-int(round(13 * f)))
        self._sfont = tkfont.Font(root=root, family=fam,
                                  size=-int(round(10 * f)), weight='bold')

        lbl = tk.Label(root, text='', font=self._font, bg=BG, fg=FG,
                       wraplength=int((WIN_W - 24) * f), justify='left',
                       anchor='w', padx=10, pady=6)
        lbl.pack(fill='both', expand=True)
        self._lbl = lbl

        root.withdraw()
        root.update_idletasks()
        _no_activate(root.winfo_id())
        self._alive = True
        self._ready.set()

        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)

        def poll():
            try:
                while True:
                    cmd = self._q.get_nowait()
                    if cmd[0] == 'quit':
                        # 用 quit() 不用 destroy()：destroy 会让 Tcl 解释器在
                        # 解释器退出时被别的线程删，报 Tcl_AsyncDelete 崩溃
                        # （bubble.py 同款写法，已验证）
                        root.quit()
                        self._alive = False
                        return
                    if cmd[0] == 'show':
                        _, x, y, text = cmd
                        w, h = int(WIN_W * f), int(WIN_H * f)
                        px = max(4, min(x - w // 2, sw - w - 4))
                        py = y - h - 20
                        if py < 4:
                            py = min(y + 24, sh - h - 4)
                        root.geometry(f'{w}x{h}+{int(px / f)}+{int(py / f)}')
                        lbl.configure(text=text or '正在听…', fg=FG)
                        root.deiconify()
                        root.lift()
                    elif cmd[0] == 'text':
                        lbl.configure(text=cmd[1] or '正在听…')
                    elif cmd[0] == 'hide':
                        root.withdraw()
            except queue.Empty:
                pass
            except Exception:
                log.error('听写小窗指令处理出错', exc_info=True)
            try:
                root.after(30, poll)
            except Exception:
                pass

        root.after(30, poll)
        try:
            root.mainloop()
        finally:
            self._alive = False


class Dictate:
    """按住 Alt 说话的总控。键盘钩子 + 录音 + 识别 + 打字都在这里收口。"""

    def __init__(self, app):
        self.app = app
        self.win = ListenWindow().start()
        self.enabled = True          # 托盘可关；启动时由 app 用偏好覆盖
        self.engine = 'local'        # local / online（online 需配置 URL）
        self._listener = None
        self._lock = threading.Lock()
        self._grace = None           # 宽限 Timer
        self._combo = False          # Alt 按住期间出现过其它键
        self._state = 'idle'         # idle / pending / recording / finishing
        self._abort = threading.Event()   # 持久取消标志（跨线程竞态安全）
        self._stop_evt = None        # 当前会话的松手/取消事件（会话线程创建）
        self._cancel_evt = None
        self._sess_thread = None
        self._audio_q = queue.Queue()
        self._stream = None
        self.stats = {'sessions': 0, 'typed': 0, 'cancelled': 0, 'empty': 0}

    # ---------------------------- 生命周期 ----------------------------

    def start(self):
        if self._listener is not None:
            return self
        from pynput import keyboard
        self._listener = keyboard.Listener(
            on_press=self._on_key, on_release=self._on_key_release)
        self._listener.start()
        log.info('语音输入就绪：按住 Alt 说话，松开打进光标处'
                 '（组合键不受影响，识别引擎=%s）', self.engine)
        return self

    def stop(self):
        self._cancel('quit')
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._listener = None
        self.win.stop()

    def toggle(self, enabled=None):
        """开关语音输入。关 = 连模型一起放掉，不留内存。"""
        self.enabled = (not self.enabled) if enabled is None else bool(enabled)
        log.info('语音输入 %s', '开' if self.enabled else '关')
        if self.enabled:
            self._warm_up()
        else:
            self._release_model()
        return self.enabled

    def _warm_up(self):
        """开着就后台把模型备好，免得第一次按住 Alt 白等 1.6 秒。

        在线引擎不加载本地模型；失败只记日志，真正说话时 ensure_loaded 会重试。
        """
        if self.engine != 'local':
            return

        def job():
            from core import asr
            try:
                asr.LOCAL.ensure_loaded()
                log.info('语音识别模型预热完成')
            except Exception as e:
                log.warning('语音识别预热失败（说话时会重试）: %s', e)

        threading.Thread(target=job, daemon=True, name='tts-asr-warm').start()

    def _release_model(self):
        """关掉语音输入就把模型放掉：不用还占着 300MB 没道理。

        先取消进行中的会话 —— 会话对象还攥着 recognizer，光置 None 只是
        减引用，内存要等会话结束才落。
        """
        from core import asr
        try:
            self._cancel('语音输入已关闭')
            if asr.LOCAL.unload():
                log.info('语音输入关闭 → 识别模型已释放')
            else:
                log.debug('语音输入关闭 → 模型本来就没加载')
        except Exception as e:
            log.warning('释放识别模型失败: %s', e)

    @property
    def state(self):
        return self._state

    def online_ready(self):
        from core import asr
        return asr.online_available()

    # ---------------------------- 键盘事件 ----------------------------

    @staticmethod
    def _is_alt(key):
        from pynput.keyboard import Key
        return key in (Key.alt, Key.alt_l, Key.alt_r)

    def _on_key(self, key):
        try:
            if self._is_alt(key):
                self._alt_down()
            else:
                self._combo_seen()
        except Exception:
            log.error('键盘事件处理异常', exc_info=True)

    def _on_key_release(self, key):
        try:
            if self._is_alt(key):
                self._alt_up()
        except Exception:
            log.error('键盘事件处理异常', exc_info=True)

    def _combo_seen(self):
        """Alt 按住期间出现其它键 = 这是组合键，不是说话。"""
        with self._lock:
            if self._state == 'pending':
                self._clear_grace()
                self._state = 'idle'
                self._combo = True
                return
        self._cancel('combo')

    def _alt_down(self):
        with self._lock:
            if self._state != 'idle' or not self.enabled or self._combo:
                return
            self._state = 'pending'
            self._combo = False
        self._grace = threading.Timer(
            max(0.05, config.ASR_PTT_GRACE_MS / 1000.0), self._begin)
        self._grace.daemon = True
        self._grace.start()

    def _alt_up(self):
        with self._lock:
            if self._state == 'pending':
                self._clear_grace()
                self._state = 'idle'
                self._combo = False
                return          # 太快松手（像组合键残留），不当成说话
        self._stop_evt and self._stop_evt.set()

    def _clear_grace(self):
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None

    def _cancel(self, why):
        """会话中途取消（组合键/退出）：丢音频、关窗、不留痕。

        ⚠ _abort 是持久标志，不是会话局部事件：_begin 先置 recording 再起线程，
        取消若落在「线程还没跑到建事件」的窗口里，会话启动后靠检查 _abort 兜住，
        不然会出现「取消了却还在录音、最后还把字打出去」的怪象。
        """
        with self._lock:
            st = self._state
            if st not in ('recording', 'finishing'):
                return
            self._state = 'finishing'
        self._abort.set()
        self._cancel_evt and self._cancel_evt.set()
        self._stop_evt and self._stop_evt.set()
        self.stats['cancelled'] += 1
        log.info('录音取消（%s）', why)

    def cancel(self):
        """给鼠标钩子用：任何鼠标键按下时若在录音，立即取消。

        这是钩子线程直接调用的，必须快：只置标志，不碰音频/窗口。
        """
        self._combo_seen()

    # ---------------------------- 会话线程 ----------------------------

    def _begin(self):
        with self._lock:
            if self._state != 'pending':
                return
            self._state = 'recording'
        threading.Thread(target=self._session, daemon=True,
                         name='tts-dictate').start()

    def _session(self):
        """一次说话会话：开麦 → 喂识别 → （Alt 松开）→ 取终稿 → 打字。

        全部主体都包在 try/finally 里：无论哪种结局（取消/出错/正常），finally
        都会把状态复位到 idle、关窗、清 abort 标志。之前 abort 竞态兜底写在
        try 外面提前 return，会让状态永远卡在 finishing、PTT 失效 —— 单测抓的。
        """
        from core import asr
        stop, cancel = threading.Event(), threading.Event()
        self._stop_evt, self._cancel_evt = stop, cancel
        x, y = _cursor_pos()
        self._audio_q = queue.Queue()
        text, t_start, sr = '', None, config.ASR_SAMPLE_RATE
        stream = sess = None
        online = False
        try:
            if self._abort.is_set():        # 取消抢在会话启动前（竞态兜底）
                return
            online = self.engine == 'online' and self.online_ready()
            if not online:
                # 首次会加载 1~2s，窗口先给个提示（关掉再开模型已卸载，会重新加载）
                if not asr.LOCAL.loaded:
                    self.win.show(x, y, '语音识别加载中（约 2 秒）…')
                sess = asr.LOCAL.session()
            self.win.show(x, y, '正在听…')
            self.stats['sessions'] += 1

            # --- 开麦（16k 优先，失败回退设备默认率，识别前重采样） ---
            def cb(indata, frames, t, status):
                self._audio_q.put(np.array(indata[:, 0], dtype=np.float32))

            import sounddevice as sd
            try:
                stream = sd.InputStream(samplerate=sr, channels=1,
                                        dtype='int16', callback=cb)
                stream.start()
            except Exception as e:
                sr = int(sd.query_devices(None, 'input')['default_samplerate'])
                log.warning('设备开不了 16k，回退 %dHz（%s）', sr, e)
                stream = sd.InputStream(samplerate=sr, channels=1,
                                        dtype='int16', callback=cb)
                stream.start()

            # --- 喂识别 + partial ---
            got_any = False
            t_start = time.time()
            while not stop.is_set() and not cancel.is_set():
                if self._abort.is_set():
                    return
                if time.time() - t_start > config.ASR_MAX_S:
                    log.info('录音达上限 %ss，按松手处理', config.ASR_MAX_S)
                    break
                try:
                    chunk = self._audio_q.get(timeout=0.25)
                except queue.Empty:
                    continue
                if not online and sess is not None:
                    sess.accept(chunk)
                    p = sess.partial()
                    if p:
                        got_any = True
                        self.win.update(p)
            stream.stop()
            stream.close()
            stream = None
            self.win.update('识别中…' if online else '松手，识别中…')

            if cancel.is_set():
                return
            # --- 取终稿 ---
            if online:
                samples = []
                while True:
                    try:
                        samples.append(self._audio_q.get_nowait())
                    except queue.Empty:
                        break
                if not samples:
                    self.stats['empty'] += 1
                    return
                text = asr.online_transcribe(np.concatenate(samples), sr)
            else:
                text = sess.finish() if sess is not None else ''
            text = (text or '').strip()
        except Exception as e:
            log.error('语音识别会话失败: %s', e, exc_info=True)
            self.app and self.app._notify(f'语音识别出错：{e}')
            return
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._stop_evt = self._cancel_evt = None
            self._abort.clear()             # 会话结束（无论哪种结局）都复位
            with self._lock:
                self._state = 'idle'
                self._combo = False
            self.win.hide()

        if self._abort.is_set():            # finally 前一刻才取消：同样不落字
            return
        if not text or len(text) < config.ASR_MIN_CHARS:
            if t_start is not None:
                log.info('没识别到文字（录音 %ss）', round(time.time() - t_start, 1))
            self.stats['empty'] += 1
            self.app and self.app._notify('没听清，再试一次')
            return
        n = type_text(text)
        self.stats['typed'] += 1
        log.info('语音输入 → 打字 %d 字（%ss 音频）：%s', n,
                 round(time.time() - t_start, 1), text[:50])
