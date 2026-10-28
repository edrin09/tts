"""本地朗读服务：127.0.0.1:9528，模型常驻，只被本机调用。

为什么用 stdlib 的 http.server
------------------------------
服务只有 9 个接口、单用户、纯本机回环，引 FastAPI/uvicorn 只多一层依赖和
一个启动检查点。ThreadingHTTPServer 的并发模型对「一个播放器顺序要块 +
偶尔并发要 health」绰绰有余。

线程模型
--------
* 每个请求一个线程（daemon）
* **模型前向全局串行**（TaskStore.synth_lock）—— torch 里多线程抢 CPU 只会更慢
* 同一任务内生成顺序执行（Task.gen_lock），不同任务可以交替但要排队抢模型
* 已生成的块读取不加锁：直接读文件，不碰模型

接口
----
GET  /health                          → 状态、内存、缓存
POST /speak   {text,voice,rate}       → 立刻返回 task_id + plan（不做合成）
GET  /block?task_id&i=N               → 第 N 块 WAV（现算；header 带 offset_s/dur_s）
GET  /state?task_id                   → 进度
POST /state   {task_id,chunk_idx,pos_s,played_s} → 上报播放进度（按采样点算出的秒）
GET  /locate?task_id&chunk_idx|pos_s  → 把块号/秒解析成绝对位置
POST /resume  {task_id,chunk_idx,pos_s}          → 从该点继续
POST /rate    {task_id,rate,chunk_idx,pos_s}     → 换倍速（同文本新任务）
POST /stop    {task_id}               → 停止合成，已生成块保留
GET  /tasks                           → 最近任务（供「接着听」）
POST /shutdown                        → 干净退出（测试/运维用）
"""
import json
import logging
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402
from service.cache import BlockCache  # noqa: E402
from service.tasks import TaskStore  # noqa: E402

log = logging.getLogger('tts.service')

STATE = {
    'engine': None,
    'store': None,
    'cache': None,
    'load_s': None,
    'started': time.time(),
    'inflight': 0,
    'inflight_lock': threading.Lock(),
    'voices': [],
}


def _rss_mb():
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:
        return None


def _threads():
    # ONNX 版没有 torch，线程数直接报配置值（就是 ORT 的 intra_op_num_threads）
    eng = STATE.get('engine')
    return getattr(eng, 'threads', None) or config.THREADS


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'tts-local/1.0'

    # ---------------------------- 基础 ----------------------------

    def log_message(self, fmt, *args):
        log.debug('%s %s', self.address_string(), fmt % args)

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _err(self, code, msg, **kw):
        self._json(dict(error=msg, **kw), code)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _query(self):
        u = urlparse(self.path)
        return u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}

    # ---------------------------- 路由 ----------------------------

    def do_GET(self):
        path, q = self._query()
        try:
            if path == '/health':
                return self._health()
            if path == '/block':
                return self._block(q)
            if path == '/state':
                return self._state(q)
            if path == '/locate':
                return self._locate(q)
            if path == '/tasks':
                return self._json(dict(tasks=STATE['store'].recent(8)))
            if path == '/':
                return self._json(dict(service='tts-local', endpoints=[
                    '/health', '/speak', '/block', '/state', '/locate', '/resume',
                    '/rate', '/stop', '/tasks']))
            return self._err(404, f'unknown path {path}')
        except Exception as e:  # 兜底，别让一个异常把连接挂死
            log.error('GET %s 失败: %s\n%s', path, e, traceback.format_exc())
            return self._err(500, f'{type(e).__name__}: {e}')

    def do_POST(self):
        path, q = self._query()
        try:
            if path == '/speak':
                return self._speak()
            if path == '/state':
                return self._progress()
            if path == '/resume':
                return self._resume()
            if path == '/rate':
                return self._rate()
            if path == '/stop':
                return self._stop()
            if path == '/shutdown':
                return self._shutdown()
            return self._err(404, f'unknown path {path}')
        except Exception as e:
            log.error('POST %s 失败: %s\n%s', path, e, traceback.format_exc())
            return self._err(500, f'{type(e).__name__}: {e}')

    # ---------------------------- 实现 ----------------------------

    def _health(self):
        c = STATE['cache']
        self._json(dict(
            loaded=bool(STATE['engine']),
            engine=STATE.get('engine_kind') or config.ENGINE,
            load_s=STATE['load_s'],
            rss_mb=_rss_mb(),
            threads=_threads(),
            queue=STATE['inflight'],
            uptime_s=round(time.time() - STATE['started'], 1),
            cache=c.stats() if c else None,
            store=STATE['store'].stats() if STATE['store'] else None,
            voices=STATE['voices'],
            rate_default=config.RATE_DEFAULT,
            rates=config.RATES,
        ))

    def _speak(self):
        b = self._body()
        text = (b.get('text') or '').strip()
        if not text:
            return self._err(400, 'text 为空')
        t0 = time.perf_counter()
        store = STATE['store']
        task = store.create(text, b.get('voice'), b.get('rate'))
        plan_ms = round((time.perf_counter() - t0) * 1000, 1)
        cached = sum(1 for c in task.plan
                     if (STATE['cache'].dir / (STATE['cache'].key(c, task.voice, task.rate) + '.wav')).is_file())
        est = round(len(text) * 0.20 / max(task.rate, 0.1), 2)
        self._json(dict(task_id=task.id, plan=task.to_dict()['plan'], chars=len(text),
                        voice=task.voice, rate=task.rate, est_audio_s=est,
                        cached_blocks=cached, chunks=len(task.plan),
                        plan_ms=plan_ms,
                        took_ms=round((time.perf_counter() - t0) * 1000, 1)))

    def _block(self, q):
        tid, i = q.get('task_id'), q.get('i')
        if not tid or i is None:
            return self._err(400, '需要 task_id 与 i')
        store = STATE['store']
        task = store.get(tid)
        if not task:
            return self._err(404, f'任务不存在: {tid}')
        i = int(i)
        with STATE['inflight_lock']:
            STATE['inflight'] += 1
        try:
            meta = store.ensure(task, i)
            f = task.block_path(i)
            data = f.read_bytes()
        finally:
            with STATE['inflight_lock']:
                STATE['inflight'] -= 1
        self._send(200, data, 'audio/wav', {
            'X-Index': meta['i'], 'X-Offset-S': meta['offset_s'], 'X-Dur-S': meta['dur_s'],
            'X-Chars': meta['chars'], 'X-Cache-Hit': 1 if meta.get('hit') else 0,
            'X-Total-S': task.total_s(), 'X-Chunks': len(task.plan), 'X-Rate': task.rate,
            'X-Gen-S': meta.get('gen_s') or 0,
        })

    def _state(self, q):
        tid = q.get('task_id')
        task = STATE['store'].get(tid) if tid else None
        if not task:
            return self._err(404, '任务不存在')
        d = task.to_dict(with_plan=False)
        d.update(next_i=len(task.blocks), total_s=task.total_s())
        self._json(d)

    def _progress(self):
        b = self._body()
        tid = b.get('task_id')
        if not tid:
            return self._err(400, '需要 task_id')
        r = STATE['store'].progress(tid, b.get('chunk_idx'), b.get('pos_s'), b.get('played_s'))
        if r is None:
            return self._err(404, '任务不存在')
        self._json(dict(ok=True, **r))

    def _locate(self, q):
        tid = q.get('task_id')
        task = STATE['store'].get(tid) if tid else None
        if not task:
            return self._err(404, '任务不存在')
        r = STATE['store'].locate(task, q.get('chunk_idx'), q.get('pos_s'))
        r['done'] = task.done
        self._json(r)

    def _resume(self):
        b = self._body()
        task = STATE['store'].get(b.get('task_id'))
        if not task:
            return self._err(404, '任务不存在')
        task.stopped = False
        task.status = 'ready'
        chunk_idx = b.get('chunk_idx')
        pos_s = b.get('pos_s')
        # 允许只给其中一个：只给 pos_s 就按时钟反查块，只给 chunk_idx 就从头读该块
        if pos_s is None and chunk_idx is None:
            chunk_idx, pos_s = task.chunk_idx, task.pos_s
        loc = STATE['store'].locate(task, chunk_idx, pos_s)
        STATE['store'].progress(task.id, loc['chunk_idx'], loc['pos_s'])
        self._json(dict(ok=True, task_id=task.id, **loc))

    def _rate(self):
        """换倍速：从「当前块」开始的剩余文本，按新速率开一个新任务。

        为什么不是「同一个任务重算所有块」
        ----------------------------------
        块的 offset 是累计时长，倍速一改，前面所有块的时长都变，位置映射就得
        先把前面全部重合成一遍（几十秒）。改成「剩余文本新任务」以后，新任务
        的第 0 块就是当前块，位置映射只需 1 块的时间，而且**不需要**阻塞播放：
        播放器可以先用旧速率接着播，等新任务首块合成好再无缝换过去。
        """
        b = self._body()
        store = STATE['store']
        task = store.get(b.get('task_id'))
        if not task:
            return self._err(404, '任务不存在')
        rate = float(b.get('rate') or config.RATE_DEFAULT)
        chunk_idx = int(b.get('chunk_idx', task.chunk_idx))
        chunk_idx = min(max(chunk_idx, 0), len(task.plan) - 1)
        tail = ''.join(task.plan[chunk_idx:]).strip()
        if not tail:
            return self._err(400, '已经到末尾，没有可续读的文本')
        t0 = time.perf_counter()
        new = store.create(tail, task.voice, rate)
        self._json(dict(ok=True, task_id=new.id, rate=rate, old_task_id=task.id,
                        from_chunk=chunk_idx, tail_chars=len(tail),
                        chunks=len(new.plan), plan=new.to_dict()['plan'],
                        took_ms=round((time.perf_counter() - t0) * 1000, 1)))

    def _stop(self):
        b = self._body()
        task = STATE['store'].get(b.get('task_id'))
        if not task:
            return self._err(404, '任务不存在')
        task.stopped = True
        task.status = 'stopped'
        STATE['store'].progress(task.id, b.get('chunk_idx'), b.get('pos_s'))
        self._json(dict(ok=True, generated=len(task.blocks), chunks=len(task.plan)))

    def _shutdown(self):
        self._json(dict(ok=True, bye=True))
        log.info('收到 shutdown，准备退出')
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def existing_service(timeout=1.5):
    """探一下端口上是不是已经有服务在跑（Windows 的 SO_REUSEADDR 允许重复绑定，
    两个实例同时监听同一个端口会让请求随机落到旧代码上，必须显式挡掉）"""
    try:
        import requests
        r = requests.get(f'http://{config.HOST}:{config.PORT}/health', timeout=timeout)
        return r.json() if r.ok else None
    except Exception:
        return None


def watch_parent(srv, pid):
    """启的是受管子进程：启动器被强杀时自己也退出，不留孤儿进程"""
    def run():
        import psutil
        while True:
            time.sleep(3.0)
            if not psutil.pid_exists(pid):
                log.warning('启动器进程 %s 已不在，服务自动退出', pid)
                srv.shutdown()
                return

    threading.Thread(target=run, daemon=True, name='parent-watch').start()


def build():
    """按 TTS_ENGINE 建引擎 + 起缓存/任务仓储，返回 (server, engine)

    online 模式不 import onnxruntime、不加载 G2P —— 这是「切换在线模型省 700MB」
    的根本原因：省内存靠的是换进程（重启服务），不是在同一进程里卸载模型。
    """
    t0 = time.perf_counter()

    if config.ENGINE == 'online':
        from core.engine_online import EdgeEngine
        eng = EdgeEngine()
        eng.load()
        eng.warmup()  # 在线引擎的预热就是连通性自检，失败只警告不拖垮启动
        load_s = round(time.perf_counter() - t0, 2)
    else:
        # 英文前端（espeak-ng）首次初始化 ~1.25s，会让「英文出现在首块」的首字从 0.86s 涨到 2.19s。
        # 在模型加载**之前**就把线程点着：这 1.25s 与模型加载（~3s）重叠，服务 ready 时英文已经就绪，
        # 可服务时间几乎不变（实测 4.05 → 4.1s）。纯中文场景也就多付这一次后台开销，不影响首字。
        en_ready = threading.Event()

        def _warm_en():
            try:
                from core import en_g2p
                t = time.perf_counter()
                en_g2p.en_g2p('API')
                log.info('英文前端就绪 %.2fs', time.perf_counter() - t)
            except Exception as e:      # 装不上 espeak 也不该拖垮服务，英文段退化成 ❓ 而已
                log.warning('英文前端预热失败：%s', e)
            finally:
                en_ready.set()

        threading.Thread(target=_warm_en, daemon=True, name='warm-en').start()

        from core.engine import Engine
        eng = Engine()
        eng.load()
        eng.warmup()  # 预热：第一次前向含 ~0.5s 一次性开销，不预热首个请求会特别慢
        en_ready.wait(3.0)  # 正常早跑完了；真卡住也不拿启动时间陪葬
        load_s = round(time.perf_counter() - t0, 2)

    # 缓存键掺入引擎名：同一个文本在两个引擎下发音/音色不同，不能互串缓存
    cache = BlockCache(config.CACHE_DIR, config.CACHE_MAX_MB,
                       f'{config.CACHE_SALT}-{config.ENGINE}')
    store = TaskStore(eng, cache, config.CACHE_DIR, config.TASK_KEEP)
    STATE.update(engine=eng, engine_kind=config.ENGINE, load_s=load_s, cache=cache,
                 store=store, voices=[config.VOICE_FEMALE, config.VOICE_MALE])
    log.info('引擎就绪 kind=%s load=%ss threads=%s rss=%sMB',
             config.ENGINE, load_s, eng.threads, _rss_mb())

    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    return srv, eng


def main(argv=None):
    argv = argv or sys.argv[1:]
    # 日志体系已拆除（不写文件、不建目录）；log.* 调用点被全局静默。
    # 需要排查时删掉这行，再给 root 加 handler 即可。
    logging.disable(logging.CRITICAL)
    h = existing_service()
    if h and h.get('loaded'):
        log.warning('端口 %s 上已有服务在跑（load_s=%s, uptime=%ss），本次启动退出',
                    config.PORT, h.get('load_s'), h.get('uptime_s'))
        return 0
    log.info('启动服务 %s:%s cache=%s max=%sMB', config.HOST, config.PORT,
             config.CACHE_DIR, config.CACHE_MAX_MB)
    srv, eng = build()
    parent = os.environ.get('TTS_PARENT_PID')
    if parent:
        watch_parent(srv, int(parent))
    log.info('监听 http://%s:%s', config.HOST, config.PORT)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log.info('KeyboardInterrupt')
    finally:
        srv.server_close()
        log.info('服务已退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
