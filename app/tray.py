"""托盘：系统级入口。菜单只留引擎/语音输入设置和系统项（开机自启/退出）——
语速、音色在划词浮窗上调，播放控制（暂停/上下句/停止）走浮窗和快捷键，
托盘一律不放。
"""
import logging
import threading
import time

log = logging.getLogger('tts.tray')


def make_icon(color=(70, 150, 240), size=64):
    """画一个喇叭图标（不依赖外部资源文件）"""
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    # 喇叭本体
    d.polygon([(8 * s, 26 * s), (20 * s, 26 * s), (32 * s, 14 * s),
               (32 * s, 50 * s), (20 * s, 38 * s), (8 * s, 38 * s)],
              fill=color + (255,))
    # 两道声波
    for i, r in enumerate((10, 15)):
        d.arc([(30 - r + 4) * s, (32 - r) * s, (30 + r + 4) * s, (32 + r) * s],
              start=-55, end=55, fill=color + (255,), width=max(1, int(3 * s)))
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = None
        self._ready = threading.Event()

    def build_menu(self):
        """菜单结构在启动时建一次；勾选/标题等状态项用 lambda 动态取值，
        值变了靠 app 层调 icon.update_menu() 刷新。"""
        import pystray
        from app.app import autostart_enabled   # 是模块级函数，不是 App 的方法

        # 二选一 radio 子菜单的通用写法：pystray 用 co_argcount 校验回调，
        # 只接受 0/1/2 个位置参数，不能写 lambda icon, item, x=r: ...
        # （整个托盘线程会挂，症状是「启动后没有图标」），一律闭包工厂。
        def switch_action(method, value):
            def act(icon, item):
                return method(value)
            return act

        # 语音引擎：切换要 10~14s，切换中由 App 把菜单项置灰挡重复点击
        # 勾选状态一律走「闭包工厂」—— pystray 用 co_argcount 校验回调，
        # 带默认参数的 lambda（lambda item, k=... ）会被当成 2 参回调传两个值，
        # 症状是整个托盘线程挂掉（图标消失），别图省事。
        def checked_engine(kind):
            return lambda item: self.app.engine_kind == kind and not self.app.switching

        def enabled_engine(item):
            return not self.app.switching

        engine_menu = pystray.Menu(
            pystray.MenuItem('Edge 在线（免费不限量）',
                             switch_action(self.app.switch_engine, 'edge'),
                             radio=True, enabled=enabled_engine,
                             checked=checked_engine('edge')),
            pystray.MenuItem('讯飞在线',
                             switch_action(self.app.switch_engine, 'xfyun'),
                             radio=True, enabled=enabled_engine,
                             checked=checked_engine('xfyun')),
            pystray.MenuItem('本地模型',
                             switch_action(self.app.switch_engine, 'local'),
                             radio=True, enabled=enabled_engine,
                             checked=checked_engine('local')))

        # 识别引擎：在线项仅在 .env 配好凭据时可点。
        # 顺序跟上面的语音引擎一致 —— 在线在上、本地模型统一放最下面
        asr_menu = pystray.Menu(
            pystray.MenuItem('讯飞听写',
                             switch_action(self.app.set_dictate_engine, 'xfyun'),
                             radio=True,
                             enabled=lambda item: bool(self.app.dictate and
                                                       self.app.dictate.online_ready()),
                             checked=lambda item: bool(self.app.dictate and
                                                       self.app.dictate.engine == 'xfyun')),
            pystray.MenuItem('本地模型',
                             switch_action(self.app.set_dictate_engine, 'local'),
                             radio=True,
                             checked=lambda item: bool(self.app.dictate and
                                                       self.app.dictate.engine == 'local')))

        # 语音输入：定向开/关（点「开」就是开，不会翻转），首次开才初始化听写
        dictate_menu = pystray.Menu(
            pystray.MenuItem('开',
                             switch_action(self.app.set_dictate_enabled, True),
                             radio=True,
                             checked=lambda item: bool(self.app.dictate and
                                                       self.app.dictate.enabled)),
            pystray.MenuItem('关',
                             switch_action(self.app.set_dictate_enabled, False),
                             radio=True,
                             checked=lambda item: bool(self.app.dictate and
                                                       not self.app.dictate.enabled)))

        return pystray.Menu(
            pystray.MenuItem(
                lambda item: ('语音引擎：切换中…' if self.app.switching else '语音引擎'),
                engine_menu,
                enabled=lambda item: not self.app.switching),
            pystray.MenuItem('识别引擎', asr_menu),
            pystray.MenuItem(
                lambda item: ('语音输入：听写中…' if (self.app.dictate and
                              self.app.dictate.state in ('recording', 'finishing')) else
                              '语音输入'),
                dictate_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('开机自启', lambda: self.app.toggle_autostart(),
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem('退出', lambda: self.app.quit()),
        )

    def run(self, block=True):
        import pystray

        try:
            self.icon = pystray.Icon('tts-speak', make_icon(),
                                     'TTS 朗读 · 划词朗读', self.build_menu())
        except Exception as e:
            # 托盘起不来不该把整个应用带下去：钩子还能用，只是没有图标可点
            log.error('托盘初始化失败：%s（快捷键仍可用）', e, exc_info=True)
            self._ready.set()
            return self
        if not block:
            self.icon.run_detached()
            self._ready.set()
            return self
        self._ready.set()
        try:
            self.icon.run()
        except Exception as e:
            log.error('托盘运行失败：%s（快捷键仍可用）', e, exc_info=True)
        return self

    def notify(self, msg, title='TTS 朗读'):
        if self.icon is not None:
            try:
                self.icon.notify(msg, title)
            except Exception:
                log.debug('通知失败: %s', msg)
