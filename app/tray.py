"""托盘：唯一的可视入口。菜单只做「控制」和「退出」，不做朗读——
朗读靠划词浮窗（选中文字松手后弹出）。
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

    def _pause_text(self, item=None):
        return '继续' if self.app.player.paused else '暂停'

    def build_menu(self):
        import pystray
        from app.app import autostart_enabled   # 是模块级函数，不是 App 的方法

        def rate_action(r):
            # 不能写成 lambda icon, item, r=r: ... —— pystray 用 co_argcount 校验
            # 回调，只接受 0/1/2 个位置参数，带默认值的第三个参数会被判非法
            # （ValueError，整个托盘线程直接挂掉，症状是「启动后没有图标」）
            def act(icon, item):
                return self.app.set_rate(r)
            return act

        rate_items = [
            pystray.MenuItem(
                f'{r:.2f}×',
                rate_action(r),
                checked=lambda item, r=r: abs(self.app.rate - r) < 1e-6,
                radio=True)
            for r in self.app.rates
        ]

        # 音色子菜单（下拉选择）：跟随引擎——本地只有男女声；在线才出现神经音色。
        # 切引擎时 App 会把本地不认识的音色复位回女声，所以这里不会出现「选了
        # 在线音色但引擎是本地」的悬空状态。
        def voice_action(v):
            def act(icon, item):
                return self.app.set_voice(v)
            return act

        voice_items = [
            pystray.MenuItem('女声（默认）', voice_action('female'), radio=True,
                             checked=lambda item: self.app.voice == 'female'),
            pystray.MenuItem('男声', voice_action('male'), radio=True,
                             checked=lambda item: self.app.voice == 'male'),
        ]
        if self.app.engine_online:
            from core.engine_online import ONLINE_VOICES
            voice_items += [
                pystray.MenuItem(label, voice_action(vid), radio=True,
                                 checked=lambda item, vid=vid: self.app.voice == vid)
                for vid, label in ONLINE_VOICES
            ]
        # 语音引擎子菜单（下拉二选一，样式与「识别引擎」一致）：点选即切换，
        # 不再是「点一下就换另一边」的开关式单项——避免想看看当前是哪个引擎
        # 却不小心把引擎切了。
        def engine_action(kind):
            def act(icon, item):
                return self.app.switch_engine(kind)
            return act

        engine_menu = pystray.Menu(
            pystray.MenuItem('在线模型',
                             engine_action('online'), radio=True,
                             enabled=lambda item: not self.app.switching,
                             checked=lambda item: self.app.engine_online
                             and not self.app.switching),
            pystray.MenuItem('本地模型',
                             engine_action('local'), radio=True,
                             enabled=lambda item: not self.app.switching,
                             checked=lambda item: not self.app.engine_online
                             and not self.app.switching))

        return pystray.Menu(
            # ---- 朗读控制 ----
            pystray.MenuItem(self._pause_text, lambda: self.app.toggle(), default=True),
            pystray.MenuItem('上一句', lambda: self.app.prev(),
                             enabled=lambda item: not self.app.switching),
            pystray.MenuItem('下一句', lambda: self.app.next(),
                             enabled=lambda item: not self.app.switching),
            pystray.MenuItem('重读当前', lambda: self.app.repeat(),
                             enabled=lambda item: not self.app.switching),
            pystray.MenuItem('接着听上次', lambda: self.app.resume_last(),
                             enabled=lambda item: not self.app.switching),
            pystray.MenuItem('停止', lambda: self.app.stop()),
            pystray.Menu.SEPARATOR,
            # ---- 朗读设置 ----
            pystray.MenuItem('倍速', pystray.Menu(*rate_items)),
            pystray.MenuItem('音色', pystray.Menu(*voice_items)),
            pystray.Menu.SEPARATOR,
            # ---- 引擎与语音输入（两个引擎放一起）----
            pystray.MenuItem(
                lambda item: ('语音引擎：切换中…' if self.app.switching else '语音引擎'),
                engine_menu,
                enabled=lambda item: not self.app.switching),
            pystray.MenuItem('识别引擎', pystray.Menu(
                pystray.MenuItem('本地模型',
                                 lambda: self.app.set_dictate_engine('local'),
                                 radio=True,
                                 checked=lambda item: bool(self.app.dictate and
                                                           self.app.dictate.engine == 'local')),
                pystray.MenuItem('在线模型',
                                 lambda: self.app.set_dictate_engine('online'),
                                 radio=True,
                                 enabled=lambda item: bool(self.app.dictate and
                                                           self.app.dictate.online_ready()),
                                 checked=lambda item: bool(self.app.dictate and
                                                           self.app.dictate.engine == 'online')))),
            pystray.MenuItem(
                lambda item: ('语音输入：听写中…' if (self.app.dictate and
                              self.app.dictate.state in ('recording', 'finishing')) else
                              ('语音输入：开（按住 Alt 说话）' if self.app.dictate
                               and self.app.dictate.enabled else
                               '语音输入：关（点击开启）')),
                lambda: self.app.toggle_dictate(),
                checked=lambda item: bool(self.app.dictate and self.app.dictate.enabled)),
            pystray.Menu.SEPARATOR,
            # ---- 系统 ----
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
