"""总入口：先确保服务在跑，再起托盘应用；退出时把服务收干净。

    python run.py                起服务 + 托盘（有控制台，日常调试用这个）
    pythonw run.py --tray        开机自启的形态（无控制台、无闪烁窗口）
    python run.py --no-tray      只起服务，托盘不要（给别的程序当后端）
    python run.py --stop         停掉正在跑的服务
    python run.py --status       看服务和自启状态
    python run.py --autostart on|off|status

为什么要分成两个进程
--------------------
模型冷启动 30s+、常驻 1.4–1.6GB。服务的职责是「模型常驻」，应用的职责是
「钩子 + 托盘」。分开以后：应用崩了重开不用等 30s；服务能单独跑（`--no-tray`）
给别的程序当后端。代价是要有人收尸，所以下面退出逻辑要么正常 shutdown、
要么 terminate，绝不留在后台变僵尸。
"""
import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / '.venv' / 'Scripts' / 'python.exe'
VENV_PYW = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'
MISSING_HINT = ('依赖都装在项目自带的 .venv 里，用系统 python 跑会缺包。\n'
                '  正确姿势：.venv\\Scripts\\python.exe run.py\n'
                '  或者先激活：.venv\\Scripts\\activate')


def _relaunch_in_venv():
    """发现跑的不是 .venv 的解释器，就换成它重跑一遍，命令行参数原样带过去。

    为什么要有这一步：Windows 上 `python run.py` 很可能命中 PATH 里的别的
    Python（uv / 微软商店 / WorkBuddy 自带的都算），那边没有 torch 和
    soundfile，一 import 就是 ModuleNotFoundError。与其让用户记完整路径，
    不如自己换过去。

    用 subprocess 而不是 os.execve：跨版本（3.14 → 3.13）execve 在这台机器上
    直接段错误（exit 139），因为它不换 Python 运行时。子进程则干净得多，
    退出码原样返回。
    """
    if os.environ.get('TTS_VENV_RELAUNCH') or not VENV_PY.is_file():
        return
    try:
        if Path(sys.executable).resolve() == VENV_PY.resolve():
            return
    except OSError:
        return
    argv0 = Path(sys.executable).name.lower()
    exe = VENV_PYW if argv0.startswith('pythonw') and VENV_PYW.is_file() else VENV_PY
    env = dict(os.environ, TTS_VENV_RELAUNCH='1')
    print(f'[run] 当前解释器 {sys.executable} 缺依赖，改用 {exe}', flush=True)
    try:
        code = subprocess.call([str(exe), str(Path(__file__).resolve())] + sys.argv[1:],
                               cwd=str(ROOT), env=env)
    except OSError as e:
        print(f'[run] 切换解释器失败（{e}）。{MISSING_HINT}', file=sys.stderr)
        return
    sys.exit(code)


_relaunch_in_venv()

from core import config  # noqa: E402

CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0
log = logging.getLogger('tts.run')

# 日志体系已拆除：不写文件、不建 logs 目录、不产生任何输出。
# 各模块的 log.* 调用点保留，被这行全局静默成空操作（连格式化都不做，零开销）。
# 将来真要查问题：删掉这行，再给 root 加一个 StreamHandler / FileHandler 即可恢复。
logging.disable(logging.CRITICAL)


def _mutex(name='TTS-Speak-single'):
    """单实例：已经在跑就直接退出，不重复装钩子。

    GetLastError 必须走 use_last_error=True + ctypes.get_last_error()：
    windll.kernel32.GetLastError() 的读数在 pythonw 下不可靠（ctypes 可能在
    两次调用之间动过线程错误码），会导致托盘模式下双开成功。
    """
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p   # 句柄 64 位，默认 c_int 会截断
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.CreateMutexW(None, False, name)
    if h and ctypes.get_last_error() == 183:     # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h)
        return None
    return h


def service_health(timeout=1.5):
    from app.client import Client
    try:
        c = Client(timeout=timeout)
        return c.health()
    except Exception:
        return None


def ensure_service(debug=False, wait_s=150):
    """服务没起就拉起来，等模型就绪再返回 (进程句柄或 None)"""
    h = service_health()
    if h and h.get('loaded'):
        log.info('复用已在跑的服务（load_s=%s，rss=%sMB）', h.get('load_s'), h.get('rss_mb'))
        return None

    cmd = [sys.executable, '-m', 'service.server'] + (['--debug'] if debug else [])
    log.info('拉起服务：%s', ' '.join(cmd))
    t0 = time.perf_counter()
    env = dict(os.environ, TTS_PARENT_PID=str(os.getpid()))
    proc = subprocess.Popen(cmd, cwd=str(ROOT), creationflags=CREATE_NO_WINDOW,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    from app.client import Client
    c = Client(timeout=2)
    while time.perf_counter() - t0 < wait_s:
        if proc.poll() is not None:
            raise RuntimeError(
                f'服务进程启动即退出（code={proc.returncode}）。'
                f'手动跑 ".venv\\Scripts\\python.exe -m service.server" 看报错')
        try:
            h = c.health()
            if h.get('loaded'):
                log.info('服务就绪，用时 %.1fs（模型 load=%ss，rss=%sMB）',
                         time.perf_counter() - t0, h.get('load_s'), h.get('rss_mb'))
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise TimeoutError(f'服务 {wait_s}s 内没就绪')


def stop_service(proc=None, wait_s=8):
    """干净收尾：循环 shutdown 直到端口没人应答为止。

    为什么要循环：Windows 的 SO_REUSEADDR 允许重复绑定（见 server.existing_service），
    可能有多个监听者，关掉一个端口上还有僵尸在答 health——那就再发
    一次 shutdown，直到清空或超时。
    """
    from app.client import Client
    c = Client(timeout=3)
    t0 = time.perf_counter()
    while service_health() and time.perf_counter() - t0 < wait_s:
        log.info('通知服务退出…')
        c.shutdown()
        time.sleep(0.5)
    if proc is not None and proc.poll() is None:
        log.warning('服务没响应 shutdown，强制结束')
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.info('服务进程已强制结束')
    if service_health():
        log.warning('端口 %s 上仍有服务在响应（疑似重复绑定的僵尸进程，建议任务管理器清理 python 进程）',
                    config.PORT)
    else:
        log.info('端口已清空，无残留服务')
    return True


# 服务进程句柄收在模块级：托盘切引擎要重启服务，watcher / 退出收尸都得看最新的
SVC = {'proc': None, 'debug': False}


def restart_service(expect_engine=None):
    """切换引擎用：按新 .env 重新拉起服务进程，返回新进程句柄。

    句柄先置 None 再停旧的 —— watcher 线程用「拿到手的进程是不是还是当前的」
    判断这次退出是不是我们干的，不置空会误报「服务进程退出」。
    expect_engine 给了就验货：重启完 health 报的引擎不对（说明端口上还有僵尸）
    就再清场重启一次。
    """
    SVC['proc'] = None
    stop_service()
    SVC['proc'] = ensure_service(debug=SVC['debug'])
    if expect_engine:
        h = service_health()
        if h and h.get('engine') != expect_engine:
            log.warning('重启后引擎=%s，期望 %s（端口上疑似有僵尸），再清场一次',
                        h.get('engine'), expect_engine)
            SVC['proc'] = None
            stop_service(wait_s=10)
            SVC['proc'] = ensure_service(debug=SVC['debug'])
    return SVC['proc']


def cmd_status():
    h = service_health()
    from app.app import autostart_enabled, autostart_command
    eng = (h or {}).get('engine') or config.ENGINE
    if h:
        print(f"服务：运行中  引擎={eng}  load={h.get('load_s')}s  rss={h.get('rss_mb')}MB  "
              f"线程={h.get('threads')}  uptime={h.get('uptime_s')}s")
        print(f"缓存：{h.get('cache')}")
        print(f"任务：{h.get('store')}")
    else:
        print(f'服务：未运行（下次启动将用 {eng} 引擎）')
    print(f"自启：{'已开启' if autostart_enabled() else '未开启'}")
    print(f"自启命令：{autostart_command()}")
    return 0


def preflight():
    """依赖没装齐时给一句人话，不要甩 traceback"""
    try:
        from app.client import Client  # noqa: F401
        return True
    except ModuleNotFoundError as e:
        print(f'[run] 缺少依赖 {e.name}（{e}）\n{MISSING_HINT}', file=sys.stderr)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description='TTS 朗读启动器')
    ap.add_argument('--tray', action='store_true', help='启动托盘（默认）')
    ap.add_argument('--no-tray', action='store_true', help='只起服务，不起托盘')
    ap.add_argument('--no-hotkeys', action='store_true', help='不装全局钩子（排障用）')
    ap.add_argument('--stop', action='store_true', help='停掉服务')
    ap.add_argument('--status', action='store_true', help='看状态')
    ap.add_argument('--autostart', choices=['on', 'off', 'status'], help='开机自启')
    ap.add_argument('--engine', choices=['local', 'online', 'status'],
                    help='切换/查看合成引擎（local=本地 ONNX, online=Edge 在线）')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args(argv)

    if not preflight():
        return 2

    if args.status:
        return cmd_status()
    if args.stop:
        stop_service()
        return 0
    if args.autostart:
        from app.app import autostart_enabled, autostart_set, autostart_command
        if args.autostart == 'status':
            print('已开启' if autostart_enabled() else '未开启')
            print(autostart_command())
            return 0
        ok = autostart_set(args.autostart == 'on')
        print('已设置' if ok else '设置失败')
        return 0 if ok else 1
    if args.engine:
        if args.engine == 'status':
            h = service_health()
            cur = (h or {}).get('engine') or config.ENGINE
            print(f'引擎：{cur}（{"在线 Edge TTS" if cur == "online" else "本地 ONNX"}）')
            if h:
                print(f'服务：运行中  rss={h.get("rss_mb")}MB')
            else:
                print('服务：未运行')
            return 0
        config.set_env('TTS_ENGINE', args.engine)
        print(f'已切换默认引擎 → {args.engine}（下次启动生效；'
              f'托盘菜单里切换会自动重启服务、立即可用）')
        return 0

    hold = _mutex()
    if hold is None:
        print('已经有一个实例在跑（划词浮窗可用），本次退出')
        return 0

    proc = ensure_service(debug=args.debug)
    SVC['proc'] = proc
    SVC['debug'] = args.debug

    from app.app import App
    app = App(enable_hotkeys=not args.no_hotkeys)
    app.start(tray=not args.no_tray)

    def _bye():
        stop_service(SVC['proc'])

    def _watch():
        """跟着 SVC['proc'] 看服务死活。

        旧的 wait 返回时不一定是真死：托盘切引擎会换句柄重启，拿到手的
        进程若已不是「当前句柄」，就说明这次退出是重启流程干的，闭嘴换新的看。
        """
        while not app.quitting:
            cur = SVC['proc']
            if cur is None:              # 重启空档：等新的句柄
                time.sleep(0.5)
                continue
            code = cur.wait()
            if app.quitting or cur is not SVC['proc']:
                continue
            log.error('服务进程退出（code=%s）', code)
            if app.tray:
                app.tray.notify('朗读服务已退出，重启程序可恢复')
            return

    threading.Thread(target=_watch, daemon=True, name='svc-watch').start()

    try:
        if args.no_tray:
            print('服务模式（Ctrl+C 退出）')
            while True:
                cur = SVC['proc']
                if cur is not None and cur.poll() is not None:
                    log.error('服务进程意外退出（code=%s），启动器一并退出', cur.returncode)
                    return cur.returncode or 1
                time.sleep(0.5)
        else:
            app.wait()
    except KeyboardInterrupt:
        log.info('收到 Ctrl+C')
    finally:
        app.quit(propagate=_bye)
    return 0


if __name__ == '__main__':
    sys.exit(main())
