"""全局触发：划词浮窗（左键时机），Alt+中键 暂停/继续，Alt+滚轮 上一句/下一句。

为什么不直接用 pynput 的 event_filter（踩过的坑，别再改回去）
----------------------------------------------------------
pynput 的 `mouse._win32.Listener.__init__` 是从
`self._options.get('event_filter', lambda msg, data: True)` 取过滤器的，
而这一版（1.8.2）的 `AbstractListener.__init__` **并不把 kwargs 存进 `_options`**。
实测：

    >>> Listener(on_click=f, event_filter=g)._options
    {}

于是传进去的过滤器被静默丢弃、回落成「永远放行」。所以这里直接装 WH_MOUSE_LL：
消息号、吞不吞（返回 1）都由自己决定。低级钩子靠消息循环派发，
必须单开一个线程跑 GetMessage。

本文件里所有回调都必须自己吞异常
--------------------------------
低级钩子回调抛异常 = 系统那边收不到 LRESULT，轻则事件丢失重则钩子被踢掉。
所以 `_callback` 整个包在 try 里，出错只记日志、照样 CallNextHookEx。
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger('tts.hotkey')

WH_MOUSE_LL = 14
HC_ACTION = 0
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
VK_MENU = 0x12

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
_user32.UnhookWindowsHookEx.restype = wt.BOOL
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.CallNextHookEx.restype = LRESULT
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.GetAsyncKeyState.restype = wt.SHORT
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetCurrentThreadId.restype = wt.DWORD


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wt.POINT),
                ('mouseData', wt.DWORD),
                ('flags', wt.DWORD),
                ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


def alt_down():
    """Alt 现在是否按着。

    用 GetAsyncKeyState 而不是「记下 keydown 事件」：用户可能先按 Alt 再右键，
    也可能右键按住了才补上 Alt，靠事件顺序判断两种都会漏。Async 状态是查询，
    跟顺序无关。
    """
    return bool(_user32.GetAsyncKeyState(VK_MENU) & 0x8000)


class Hotkeys:
    def __init__(self, on_toggle=None, on_prev=None, on_next=None,
                 on_select=None, on_mouse_down=None, on_any_button=None):
        self.on_toggle = on_toggle
        self.on_prev = on_prev
        self.on_next = on_next
        # 浮窗用：左键松开（可能是划词结束）与左键按下（点别处收起浮窗）
        self.on_select = on_select
        self.on_mouse_down = on_mouse_down
        # 语音输入用：任何鼠标键按下都通知一声（Alt 按住时 = 这是组合键，取消录音）。
        # 回调必须快（只置标志），它在钩子线程里直接跑
        self.on_any_button = on_any_button

        self.seen = 0         # 回调见过的鼠标键事件总数，排障用
        self.alt_misses = 0   # 见到中键/滚轮但 Alt 没按着的次数
        self.ready = threading.Event()
        self.working = False  # 钩子是否真的装上了

        self._thread = None
        self._hook = None
        self._tid = 0
        self._proc = None     # 必须持一份引用，否则被 GC 掉钩子立刻失效
        self._swallow_m = False

    # ---------------------------- 内部 ----------------------------

    def _spawn(self, fn, *a):
        if fn is None:
            return

        def run():
            try:
                fn(*a)
            except Exception as e:
                log.error('触发回调失败: %s', e, exc_info=True)

        threading.Thread(target=run, daemon=True, name='tts-trigger').start()

    def _next(self, ncode, wparam, lparam):
        return _user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _callback(self, ncode, wparam, lparam):
        if ncode != HC_ACTION:
            return self._next(ncode, wparam, lparam)
        try:
            msg = int(wparam)

            # 任何鼠标键按下/滚轮都先告诉语音输入一声：它自己判断当前有没有
            # 在录音/宽限期，没有就是 no-op。直接调（不 spawn）：置标志级快
            if (msg in (WM_LBUTTONDOWN, WM_MBUTTONDOWN, WM_MOUSEWHEEL)
                    and self.on_any_button is not None):
                try:
                    self.on_any_button()
                except Exception:
                    log.error('on_any_button 回调异常', exc_info=True)

            # 左键：只用来给浮窗提供时机（划词结束弹、点别处收），永不吞事件
            if msg == WM_LBUTTONDOWN or msg == WM_LBUTTONUP:
                if self.on_select is None and self.on_mouse_down is None:
                    return self._next(ncode, wparam, lparam)
                pt = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
                x, y = int(pt.x), int(pt.y)
                if msg == WM_LBUTTONDOWN:
                    self._spawn(self.on_mouse_down, x, y)
                else:
                    self._spawn(self.on_select, x, y)
                return self._next(ncode, wparam, lparam)

            if msg == WM_MBUTTONDOWN:
                self.seen += 1
                if not alt_down():
                    self.alt_misses += 1
                    return self._next(ncode, wparam, lparam)
                self._spawn(self.on_toggle)
                self._swallow_m = True
                return 1

            if msg == WM_MBUTTONUP:
                # 中键 down 吞了，up 也得吞，否则有的应用会把中键 up 当粘贴
                if self._swallow_m:
                    self._swallow_m = False
                    return 1
                return self._next(ncode, wparam, lparam)

            if msg == WM_MOUSEWHEEL:
                if not alt_down():
                    return self._next(ncode, wparam, lparam)
                data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
                if delta > 0:
                    self._spawn(self.on_prev)
                elif delta < 0:
                    self._spawn(self.on_next)
                return 1

            return self._next(ncode, wparam, lparam)
        except Exception:
            log.error('鼠标钩子回调异常（已忽略，钩子保持存活）', exc_info=True)
            return self._next(ncode, wparam, lparam)

    def _run(self):
        self._proc = HOOKPROC(self._callback)
        hmod = _kernel32.GetModuleHandleW(None)
        self._hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)
        if not self._hook:
            err = ctypes.get_last_error()
            log.error('装鼠标钩子失败（WinError %s）：%s',
                      err, ctypes.FormatError(err).strip())
            self.ready.set()
            return
        self._tid = _kernel32.GetCurrentThreadId()
        self.working = True
        self.ready.set()
        log.debug('鼠标钩子线程 %s 就绪', self._tid)

        msg = wt.MSG()
        while True:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:          # 0 = WM_QUIT，-1 = 出错
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self.working = False
        log.debug('鼠标钩子已卸载')

    # ---------------------------- 生命周期 ----------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name='tts-mousehook',
                                        daemon=True)
        self._thread.start()
        if not self.ready.wait(3.0):
            log.error('鼠标钩子 3 秒内没装好')
            return self
        if self.working:
            log.info('触发钩子就绪：划词浮窗 / Alt+中键 暂停 / Alt+滚轮 上下句')
        return self

    def stop(self):
        if self._hook and self._tid:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(1.5)
        self._thread = None
        self._hook = None
        self.working = False

    def status(self, limit=6):
        """排障用：钩子活着没、见过多少事件"""
        return dict(working=self.working, seen=self.seen,
                    alt_misses=self.alt_misses, thread=self._tid,
                    recent=list(self._recent[-limit:]) if hasattr(self, '_recent') else [])

