"""应用装配：触发 → 取选区 → 播放器 → 托盘，加上自启与退出。

一次朗读的完整链路（FSM 只有三态，故意做得简单）

    idle --划词浮窗--> 取选区 --有文字--> speak(新 task) --首块到--> playing
      ^                                        |                      |
      |-- 停止/播完 ---------------------------+----- 暂停 <-----> playing
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import winreg
from pathlib import Path

from core import config

log = logging.getLogger('tts.app')

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'TTS-Speak'


# ---------------------------- 开机自启 ----------------------------

def _pythonw():
    exe = Path(os.sys.executable)
    pw = exe.with_name('pythonw.exe')
    return str(pw if pw.is_file() else exe)


def _run_py():
    return str(Path(__file__).resolve().parent.parent / 'run.py')


def autostart_command():
    return f'"{_pythonw()}" "{_run_py()}" --tray'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def autostart_set(on=True):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return autostart_enabled() == on
    except OSError as e:
        log.error('写自启注册表失败: %s', e)
        return False


# ---------------------------- 应用 ----------------------------

class App:
    def __init__(self, client=None, sink='auto', device=None, enable_hotkeys=True):
        from app.client import Client
        from app.player import Player

        self.client = client or Client()
        self.player = Player(self.client, sink=sink, device=device, on_event=self._on_event)
        # 当前倍速。没在播的时候也要记住它 —— 浮窗点自定义语速时若没有正在读的
        # 任务，player 那边无处可切（返回 no_task），但用户的意思是「下次用这个
        # 速度念」，所以偏好存在 App 这一层，speak 时带下去；并落盘，重开还在。
        self.rate = self._load_pref()
        # 当前引擎。以服务 health 实报为准（_sync_engine），config.ENGINE 只是初值。
        # 必须放在音色之前 —— 音色要按引擎的可选表做清洗（_load_voice_pref）
        self._engine = config.ENGINE
        # 当前音色。本地只有男女声；讯飞/Edge 各有自己的音色表（见 config.voice_choices）。
        # 存盘逻辑与倍速同一份 pref.json，重开还在。
        self.voice = self._load_voice_pref()
        # 引擎切换进行中的标记：切换要 10~14s，期间托盘的引擎项置灰，
        # 重复点击一律挡掉（半路再点会跟 restart_service 抢服务进程）
        self._switching = False
        self._hotkeys = None
        self._enable_hotkeys = enable_hotkeys
        self.tray = None
        self.bubble = None
        self.dictate = None
        self._sel_seq = 0
        self._quitting = threading.Event()
        self._t_trigger = None
        self.stats = {'triggers': 0, 'spoken': 0, 'empty': 0, 'bubble': 0,
                      'first_audio_ms': []}

    # ---------------------------- 事件 ----------------------------

    def _on_event(self, name, payload):
        if name == 'first_audio':
            if self._t_trigger is not None:
                ms = round((time.perf_counter() - self._t_trigger) * 1000)
                self.stats['first_audio_ms'].append(ms)
                log.info('选中→出声 %sms（选区+合成+首块）', ms)
        elif name == 'error':
            log.error('播放错误: %s', payload)
            self._notify(f'播放出错：{payload}')

    def _notify(self, msg):
        if self.tray:
            self.tray.notify(msg)
        else:
            log.info(msg)

    # ---------------------------- 选中浮窗 ----------------------------

    def _bubble_clicked(self, text):
        """点了浮窗上的「朗读」。text 是弹窗那一刻取到的，不用再取一次。"""
        self.stats['bubble'] += 1
        log.info('浮窗点了朗读：%d 字', len(text))
        self.read_selection(text=text, how='bubble')

    def _bubble_state(self):
        """浮窗用来决定「暂停/继续」「哪一档倍速亮着」「音色下拉显示什么」。

        只读本地字段，不发请求 —— 它会在 Tk 线程里被调用，不能有 IO。
        """
        st = self.player.status()
        return {'rate': st.get('rate') if st.get('task_id') else self.rate,
                'paused': bool(st.get('paused')),
                'voice': self.voice,
                'voices': self._voice_options()}

    def _voice_options(self):
        """当前引擎下可选的音色 [(key, label)]：本地男女声，讯飞给免费发音人表。"""
        return config.voice_choices()

    def _on_mouse_down(self, x, y):
        """左键按下：点在浮窗外就收起来。

        点在浮窗上时**必须放行**——全局钩子比应用的鼠标消息先到，这里要是先
        hide 了，tkinter 还没收到这次点击窗口就没了，点了等于没点。
        """
        if self.bubble is not None and not self.bubble.contains(x, y):
            self.bubble.hide()

    def _on_select(self, x, y):
        """左键松开：可能刚划完词，取到字就在鼠标旁弹浮窗。

        这里**只走 UIA，不走 Ctrl+C 兜底**：兜底会动用户剪贴板，而划词弹窗是
        高频动作（每选一次都触发一次），不能每次选中都去改剪贴板。UIA 拿不到
        （Electron 里的一部分控件、远程桌面）就不弹。

        取到空必须重试：
        Chromium/Electron 的 UIA 无障碍树是惰性构建的——重启后第一次查询
        只会触发建树，建好之前 GetSelection 一律返回空。单次尝试 + 静默
        放弃 = 用户划词没反应；实测同一个选区，失败后 14 秒再查就能拿到
        819 字（6ms）。所以按 0.35/0.8/1.5s 退避重试，把建树窗口盖过去。
        重试期间用户又点了鼠标（seq 变了）就立刻作废，不拖泥带水。
        """
        if self.bubble is None:
            return
        if time.time() - self.bubble.last_click < 0.6:
            return                      # 刚点过浮窗，这次松手是那次点击的尾巴
        self._sel_seq += 1
        seq = self._sel_seq
        time.sleep(config.BUBBLE_DELAY_MS / 1000.0)
        if seq != self._sel_seq:
            return                      # 延迟期间又点了一下，这次作废
        if self.bubble.contains(x, y):
            return
        from app import selection
        t0 = time.perf_counter()
        text, how = '', ''
        for attempt, backoff in enumerate((0, 0.35, 0.8, 1.5)):
            if backoff:
                time.sleep(backoff)
            if seq != self._sel_seq:
                return                  # 重试期间又有新动作，本次作废
            try:
                text, how = selection.uia_selection()
            except Exception as e:
                log.debug('划词取选区失败（第 %d 次）: %s', attempt + 1, e)
                text = ''
                continue
            if text:
                break
        if seq != self._sel_seq:
            return
        text = (text or '').strip()
        if len(text) < config.BUBBLE_MIN_CHARS:
            self.bubble.hide()
            return
        self.bubble.show(text, x, y)
        log.info('浮窗弹出：%d 字（%s）取字 %.0fms（第 %d 次尝试）', len(text),
                 how or '?', (time.perf_counter() - t0) * 1000, attempt + 1)

    # ---------------------------- 朗读 ----------------------------

    def _prewarm_uia(self):
        """启动期预热 UIA（后台线程）。

        做两件事：把 comtypes 的导入/COM 初始化挪出「划词那一瞬间」；顺便用
        一次查询去戳一下鼠标所在的和前台的应用，让 Chromium/Electron 现在就
        开始建无障碍树，而不是等用户第一次划词才开始。
        """
        try:
            from app import selection
            selection.uia_selection()
        except Exception as e:
            log.debug('UIA 预热失败（只影响首次划词的快慢）: %s', e)

    def read_selection(self, text, how='bubble'):
        """把浮窗取到的选区文本交给播放器朗读。"""
        self.stats['triggers'] += 1
        t0 = time.perf_counter()
        if not text:
            self.stats['empty'] += 1
            log.info('没取到选中文字（%s）', how or 'unknown')
            self._notify('没选中文字')
            return dict(ok=False, reason='empty', method=how)
        self._t_trigger = t0
        # 音色要跟当前引擎对得上：选了 Edge 的 Neural 名又切回本地/讯飞时，兜底回默认
        voice = self.voice
        if not config.voice_ok(voice, self._engine):
            voice = config.VOICE_DEFAULT
        try:
            info = self.player.speak(text, voice=voice, rate=self.rate)
        except Exception as e:
            log.error('朗读失败: %s', e, exc_info=True)
            self._notify(f'服务不可用：{e}')
            return dict(ok=False, reason=str(e))
        dt = round((time.perf_counter() - t0) * 1000)
        log.info('触发 [%s] %d 字 → task=%s 块=%s 请求%sms%s',
                 how, len(text), info.get('task_id'), info.get('chunks'), dt,
                 f'（{info["ignored"]}）' if info.get('ignored') else '')
        if info.get('ignored'):
            return info
        self.stats['spoken'] += 1
        self._refresh_tray()
        return info

    def toggle(self):
        ok = self.player.toggle()
        self._refresh_tray()
        return ok

    def prev(self):
        r = self.player.prev_sentence()
        self._refresh_tray()
        return r

    def next(self):
        r = self.player.next_sentence()
        self._refresh_tray()
        return r

    def _pref_file(self):
        return config.CACHE_DIR / 'pref.json'

    def _load_pref(self):
        """上次用的倍速。预设档位或范围内的自定义值都收；读不到/越界回默认。"""
        try:
            d = json.loads(self._pref_file().read_text(encoding='utf-8'))
            r = float(d.get('rate'))
            if config.RATE_MIN <= r <= config.RATE_MAX:
                return r
        except Exception:
            pass
        return config.RATE_DEFAULT

    def _load_voice_pref(self):
        """上次用的音色。读不到就用配置默认（female）。

        跨引擎会留下不认识的音色名（比如 Edge 的 zh-CN-XiaoxiaoNeural 切到讯飞、
        本地的 zf_001 切到 Edge），这里按**当前引擎的可选表**清洗一次，
        免得每次朗读都吃一次端点拒绝/静默降级。
        """
        try:
            d = json.loads(self._pref_file().read_text(encoding='utf-8'))
            v = str(d.get('voice') or '').strip()
            if v and config.voice_ok(v, self._engine):
                return v
        except Exception:
            pass
        return config.VOICE_DEFAULT

    # ---------------------------- 语音输入 ----------------------------

    def _start_dictate(self):
        """起语音输入监听。失败不拖累主程序（朗读还能用）。"""
        try:
            from app.dictate import Dictate
            d = Dictate(self)
            d.enabled = self._load_asr_pref()
            d.engine = self._load_asr_engine_pref()
            d.start()
            self.dictate = d
            log.info('语音输入已就绪（开关=%s 引擎=%s）', d.enabled, d.engine)
        except Exception as e:
            log.error('语音输入初始化失败（不影响朗读）: %s', e, exc_info=True)
            self.dictate = None

    def _on_any_button(self):
        """鼠标钩子的通知：任何鼠标键按下都转给听写状态机（组合键 → 取消录音）。"""
        if self.dictate is not None:
            self.dictate.cancel()

    def set_dictate_enabled(self, on):
        """托盘子菜单用：直接设开/关（toggle 的定向版，状态没变就不动）。
        首次点「开」时才初始化听写模块，失败不拖累朗读。"""
        if self.dictate is None:
            self._start_dictate()
            if self.dictate is None:
                self._notify('语音输入不可用（初始化失败，看日志）')
                return False
        changed = bool(self.dictate.enabled) != bool(on)
        if changed:
            self.dictate.toggle()
            self._save_pref()
            self._refresh_tray()
            if on:
                self._notify('语音输入已开：按住 Alt 说话，松开打进光标处')
            else:
                self._notify('语音输入已关，识别模型已从内存释放')
        return bool(self.dictate.enabled)

    def set_dictate_engine(self, engine):
        """切识别引擎：local（本地 SenseVoice）/ xfyun（讯飞听写）。

        切到 xfyun 时顺手把本地模型放掉（省 299MB）—— 不留着占内存，
        反正切回来时 _warm_up 会在后台重新加载。
        """
        if self.dictate is None:
            return dict(ok=False, reason='disabled')
        if engine == 'xfyun' and not self.dictate.online_ready():
            self._notify('讯飞听写未配置：在 .env 里填 TTS_XFYUN_APPID / API_KEY / API_SECRET')
            return dict(ok=False, reason='not_configured')
        self.dictate.engine = engine
        if engine != 'local':
            try:
                from core import asr
                asr.LOCAL.unload()
            except Exception:
                pass
        else:
            self.dictate._warm_up()
        self._save_pref()
        self._refresh_tray()
        label = '本地模型' if engine == 'local' else '讯飞听写（在线）'
        self._notify(f'语音识别引擎 → {label}')
        return dict(ok=True, engine=engine)

    def _load_asr_pref(self):
        try:
            d = json.loads(self._pref_file().read_text(encoding='utf-8'))
            return bool(d.get('dictate', True))
        except Exception:
            return True

    def _load_asr_engine_pref(self):
        try:
            d = json.loads(self._pref_file().read_text(encoding='utf-8'))
            v = str(d.get('asr_engine') or '').strip().lower()
            if v in ('online', 'edge'):
                v = 'xfyun'                     # 旧版本存的 online 就是「在线识别」
            if v in ('local', 'xfyun'):
                return v
        except Exception:
            pass
        return config.ASR_ENGINE

    def _save_pref(self):
        try:
            p = self._pref_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {'rate': self.rate, 'voice': self.voice,
                 'dictate': bool(self.dictate.enabled) if self.dictate else True,
                 'asr_engine': self.dictate.engine if self.dictate else 'local'},
                ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            log.debug('偏好保存失败: %s', e)

    def set_rate(self, rate):
        """切换倍速。正在播 → 无缝换挡；没在播 → 只记下偏好，下次朗读用它。"""
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return dict(ok=False, reason='bad_rate')
        self.rate = r
        self._save_pref()
        st = self.player.status()
        if not st.get('task_id'):
            log.info('倍速偏好 → %.2f×（当前没有在朗读，下次生效）', r)
            self._refresh_tray()
            return dict(ok=True, rate=r, pref_only=True)
        try:
            r = self.player.set_rate(r)
        except Exception as e:
            log.error('切倍速失败: %s', e)
            return dict(ok=False, reason=str(e))
        log.info('倍速 → %.2f×（耗时 %ss）', rate, r.get('took_s'))
        self._refresh_tray()
        return r

    def _voice_label(self, v):
        """音色显示名：逻辑名带上该引擎的实际音色，具体音色查表。"""
        kind = self._engine
        if v == 'female':
            if kind == 'xfyun':
                return '女声（%s）' % config.XFYUN_VOICE_FEMALE
            if kind == 'edge':
                return '女声（%s）' % config.EDGE_VOICE_FEMALE
            return '女声'
        if v == 'male':
            if kind == 'xfyun':
                return '男声（%s）' % config.XFYUN_VOICE_MALE
            if kind == 'edge':
                return '男声（%s）' % config.EDGE_VOICE_MALE
            return '男声'
        return dict(config.voice_choices(kind)).get(v, v)

    def set_voice(self, voice):
        """切换音色（下次朗读生效，正在播的不打断）。音色表跟着引擎走。"""
        v = str(voice or '').strip()
        if not v:
            return dict(ok=False, reason='bad_voice')
        if not config.voice_ok(v, self._engine):
            self._notify(f'这个音色在当前引擎（{config.ENGINE_LABELS.get(self._engine, self._engine)}）下不可用')
            return dict(ok=False, reason='bad_voice_for_engine')
        self.voice = v
        self._save_pref()
        log.info('音色 → %s（下次朗读生效）', v)
        self._notify(f'音色已切换：{self._voice_label(v)}')
        self._refresh_tray()
        return dict(ok=True, voice=v)

    # ---------------------------- 引擎切换 ----------------------------

    @property
    def engine_edge(self):
        return self._engine == 'edge'

    @property
    def engine_kind(self):
        """当前引擎：local / xfyun / edge（托盘勾选与音色校验都按它）"""
        return self._engine

    def _sync_engine(self):
        """从服务 health 同步当前引擎；服务没起来就保持原值"""
        try:
            h = self.client.health()
            if h and h.get('engine'):
                self._engine = h['engine']
        except Exception:
            pass
        return self._engine

    @property
    def switching(self):
        return self._switching

    # 三个引擎的循环顺序：托盘只传明确值，这个循环只给「不带参数调用」兜底
    _ENGINE_CYCLE = ('local', 'xfyun', 'edge')

    def switch_engine(self, kind=None):
        """本地 / 讯飞 / Edge 一键切换：写 .env → 重启服务进程 → 播报结果。

        为什么是重启进程而不是就地换引擎：省内存靠的是新进程不加载 ONNX
        （本地 ~820MB → 在线 ~100MB），同一进程里卸不掉 onnxruntime 的 arena。
        切换是异步的（本地引擎要 ~10s），结果走托盘气泡通知。
        """
        if self._switching:
            self._notify('正在切换中，等切换完成（气泡通知）后再点')
            return
        cur = self._sync_engine()
        if kind:
            target = str(kind).strip().lower()
        else:
            i = self._ENGINE_CYCLE.index(cur) if cur in self._ENGINE_CYCLE else 0
            target = self._ENGINE_CYCLE[(i + 1) % len(self._ENGINE_CYCLE)]
        if target not in config.ENGINE_LABELS:
            self._notify(f'未知引擎：{target}')
            return
        if target == cur:
            self._notify(f'当前已是{config.ENGINE_LABELS[target]}引擎')
            return
        self._switching = True
        self._refresh_tray()        # 托盘引擎项立刻变「切换中…」并置灰
        self._notify(f'正在切换到{config.ENGINE_LABELS[target]}，服务重启中…')
        threading.Thread(target=self._do_switch, args=(target,), daemon=True,
                         name='engine-switch').start()

    def _do_switch(self, target):
        try:
            self.player.stop()          # 别让播放器还朝旧服务要块
            config.set_env('TTS_ENGINE', target)
            import run                  # 延迟导入：run.py 里才会 import 本模块
            run.restart_service(expect_engine=target)
            self._sync_engine()
            h = self.client.health()
            log.info('引擎已切换 → %s（load=%ss rss=%sMB）',
                     self._engine, h.get('load_s'), h.get('rss_mb'))
            if self._engine != target:
                self._notify(f'切换未生效（服务报的是 {self._engine}），看看日志')
                return
            # 音色跟着引擎走：zf_001 在 Edge 上、zh-CN-XiaoxiaoNeural 在讯飞上都会被拒，
            # 统一按目标引擎的可选表清洗一遍，别让第一次朗读就失败
            if not config.voice_ok(self.voice, target):
                log.info('音色复位 → %s（%s 的音色表见 config.voice_choices）',
                         config.VOICE_DEFAULT, target)
                self.voice = config.VOICE_DEFAULT
                self._save_pref()
            self._notify(f'已切换到{config.ENGINE_LABELS[target]}'
                         f'（内存 {h.get("rss_mb")}MB），直接划词即可')
        except Exception as e:
            log.error('切换引擎到 %s 失败: %s', target, e, exc_info=True)
            self._notify(f'切换失败：{e}（服务可能没起来，重启程序恢复）')
        finally:
            self._switching = False
            self._refresh_tray()        # 恢复引擎菜单项可用

    # ---------------------------- 托盘 / 生命周期 ----------------------------

    def toggle_autostart(self):
        on = not autostart_enabled()
        ok = autostart_set(on)
        self._refresh_tray()
        return ok

    def _refresh_tray(self):
        if self.tray and self.tray.icon:
            try:
                self.tray.icon.update_menu()
            except Exception:
                pass

    def start(self, tray=True):
        from app.hotkey import Hotkeys
        from app.tray import Tray

        if self._enable_hotkeys:
            # UIA 预热：冷进程第一次 import comtypes/uiautomation 要几百毫秒，
            # Chromium/Electron 还要等 UIA 客户端连上来才开始建无障碍树。
            # 把这两件事都挪到启动期，划词首查就不会撞上「树还没建好」。
            threading.Thread(target=self._prewarm_uia, daemon=True,
                             name='uia-prewarm').start()
            if config.BUBBLE:
                from app.bubble import Bubble
                self.bubble = Bubble(on_click=self._bubble_clicked,
                                     on_toggle=self.toggle,
                                     on_rate=self.set_rate,
                                     on_voice=self.set_voice,
                                     get_state=self._bubble_state,
                                     rate_min=config.RATE_MIN,
                                     rate_max=config.RATE_MAX,
                                     hide_ms=config.BUBBLE_HIDE_MS).start()
                if getattr(self.bubble, '_alive', False):
                    log.info('选中浮窗已就绪：划词松手后在鼠标旁弹出'
                             '「朗读 + 倍速 + 音色」面板（%dms 后自动消失）',
                             config.BUBBLE_HIDE_MS)
                else:
                    log.warning('浮窗没能启动（缺 tkinter？），划词朗读不可用')
                    self.bubble = None
        self._hotkeys = Hotkeys(
            on_toggle=self.toggle, on_prev=self.prev, on_next=self.next,
            on_select=self._on_select if self.bubble else None,
            on_mouse_down=self._on_mouse_down if self.bubble else None,
            on_any_button=self._on_any_button).start()
        # 语音输入：按住 Alt 说话，松开识别并打进光标处。放在热键之后起监听，
        # 避免自检注入的按键被当成说话
        if config.ASR_ENABLE:
            self._start_dictate()
        if tray:
            self.tray = Tray(self)
            threading.Thread(target=self.tray.run, kwargs={'block': False},
                             daemon=True, name='tray').start()
            time.sleep(0.3)
        log.info('应用已就绪')
        return self

    def wait(self):
        """主线程挂在这里（托盘在自己的线程里跑）"""
        while not self._quitting.is_set():
            time.sleep(0.5)

    def quit(self, propagate=None):
        """退出：先停播放，再让调用方收掉服务进程"""
        self._quitting.set()
        log.info('退出中…')
        if self._hotkeys:
            self._hotkeys.stop()
        if self.bubble:
            self.bubble.stop()
        if self.dictate:
            self.dictate.stop()
        try:
            self.player.stop(notify_server=True)
        except Exception:
            pass
        self.player.close()
        if self.tray and self.tray.icon:
            try:
                self.tray.icon.stop()
            except Exception:
                pass
        if propagate:
            propagate()

    @property
    def quitting(self):
        return self._quitting.is_set()
