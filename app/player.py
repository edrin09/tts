"""流式播放器：拉块 → 绝对时间轴缓冲 → sounddevice 回调出声。

三条设计约束
------------
1. **时间轴用采样点，不用块号**
   `_pos` 是「任务内第几个采样点」。块自带 `offset_s`，于是暂停/续播/跳句
   全都只是改 `_pos`，不需要重建缓冲，也不需要重新合成。
2. **暂停 = 回调输出静音且不推进 `_pos`**
   音频流不停、音频不丢，继续时零成本（<50ms，实测就是 1 个 callback block）。
3. **不足就补静音并记 underrun**
   这既是断流统计，也是「0 次断流」的测量口径：正常播放时
   underrun 必须一直是 0（块间那 50ms 是时间轴上的空隙，走 silence 计数，不算断流）。
"""
import logging
import threading
import time
from collections import deque

import numpy as np

from core import config

log = logging.getLogger('tts.player')

BLOCK_SAMPLES = 480  # 20ms @24k：暂停响应 ≤50ms 的前提


class SoundSink:
    """真实声卡输出。回调里只做「从播放器取 n 个采样」，不做任何 IO。"""

    kind = 'sound'

    def __init__(self, sr, pull, device=None, blocksize=BLOCK_SAMPLES):
        self.sr = sr
        self.pull = pull
        self.device = device
        self.blocksize = blocksize
        self.status = ''
        self._stream = None

    def start(self):
        import sounddevice as sd

        def cb(outdata, frames, time_info, status):
            if status:
                self.status = str(status)
            out, _ = self.pull(frames)
            outdata[:, 0] = out[:frames]

        self._stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype='float32',
                                       blocksize=self.blocksize, callback=cb,
                                       device=self.device, latency='low')
        self._stream.start()
        log.info('音频流已打开 dev=%s sr=%s block=%s', self.device, self.sr, self.blocksize)

    def stop(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception as e:
                log.warning('关闭音频流失败: %s', e)

    @property
    def active(self):
        return self._stream is not None

    def info(self):
        return dict(kind='sound', device=self.device, blocksize=self.blocksize,
                    status=self.status)


class NullSink:
    """无设备时的等价物：按墙钟节奏消费（测试/无声环境用）。

    和真实声卡一样，时间是硬约束 —— 所以 underrun / 暂停 / 续播的测量口径完全一致。
    """

    kind = 'null'

    def __init__(self, sr, pull, blocksize=BLOCK_SAMPLES):
        self.sr = sr
        self.pull = pull
        self.blocksize = blocksize
        self._stop = threading.Event()
        self._th = None

    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True, name='null-sink')
        self._th.start()

    def _loop(self):
        dt = self.blocksize / self.sr
        nxt = time.perf_counter()
        while not self._stop.is_set():
            self.pull(self.blocksize)
            nxt += dt
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()  # 落后了就重新对齐，不追帧

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=1.0)
            self._th = None

    @property
    def active(self):
        return self._th is not None and self._th.is_alive()

    def info(self):
        return dict(kind='null', blocksize=self.blocksize)


class Player:
    def __init__(self, client, sr=None, prefetch_s=None, sink='auto', device=None,
                 on_event=None, buffer_high_s=None):
        self.client = client
        self.sr = sr or config.SAMPLE_RATE
        self.prefetch_s = config.PREFETCH_S if prefetch_s is None else prefetch_s
        # 高水位：只要缓冲没到这个数就继续拉块。只按 3s 预取的话，60 字块
        # 要 5s 合成，必然在块交界处断流（实测 500 字文本断流 5s）
        self.buffer_high_s = (config.BUFFER_HIGH_S if buffer_high_s is None
                              else buffer_high_s)
        self.gate_max_s = config.START_GATE_MAX_S
        self.on_event = on_event
        self._lock = threading.RLock()
        self._buf = deque()      # (start_sample, np.ndarray, index)
        self._ranges = []        # (index, offset_s, dur_s)
        self._pos = 0
        self._init_pos = 0
        self._paused = False
        self._task = None
        self._plan = []
        self._gen = 0
        self._closing = False
        self._has_audio = False
        self._fetch = None
        self._reporter = None
        self._done = True
        self.underruns = 0
        self.silence_samples = 0
        self.first_block_at = None
        self.last_rate_swap = None
        self._end_samples = 0       # 任务总采样数（末块 offset+dur），用来判定"真的播完了"
        self._gate_s = 0.0          # 起播门槛：攒够这么多缓冲才出声
        self._gate_deadline = 0.0
        self._per_char = None       # 每字合成秒（本任务实测，用于估下一块）
        self.gate_waits = []        # 每次起播实际等的秒数（统计用）
        self.underrun_events = []   # 断流发生时的播放位置（秒），最多记 40 条
        self._t_attach = 0.0
        self._t_first_audio = None
        if sink == 'null':
            self.sink = NullSink(self.sr, self._pull)
        elif sink is None:
            self.sink = None
        else:
            self.sink = SoundSink(self.sr, self._pull, device=device)

    # ---------------------------- 属性 ----------------------------

    @property
    def playing(self):
        return self._task is not None and not self._paused and not self._done

    @property
    def paused(self):
        return self._paused

    @property
    def task_id(self):
        return self._task['task_id'] if self._task else None

    @property
    def pos_s(self):
        with self._lock:
            return round(self._pos / self.sr, 3)

    # ---------------------------- 音频回调 ----------------------------

    def _pull(self, n):
        if self._paused or self._task is None:
            return np.zeros(n, np.float32), 0
        return self._render(n)

    def _render(self, n):
        out = np.zeros(n, np.float32)
        filled = 0
        first = False
        with self._lock:
            # 起播门槛：估计下一块合成不回来时，先攒缓冲再出声（比断流好听；
            # 上限 gate_max_s，宁可断流也不让首字无限期变慢）
            if not self._has_audio and self._gate_s > 0:
                avail = sum(max(0, st + len(a) - max(st, self._pos))
                            for st, a, _ in self._buf) / self.sr
                if avail < self._gate_s and time.perf_counter() < self._gate_deadline:
                    return out, 0
            while filled < n:
                if not self._buf:
                    # 播到末尾之后没数据是正常的，不能算断流（必须排除这一段）
                    if (self._has_audio and not self._paused
                            and self._pos < self._end_samples):
                        self.underruns += 1  # 该出声时没数据 = 断流
                        if len(self.underrun_events) < 40:  # 留痕，便于定位是哪一块供不上
                            self.underrun_events.append(round(self._pos / self.sr, 2))
                    break
                start, arr, _idx = self._buf[0]
                end = start + arr.shape[0]
                if self._pos >= end:
                    self._buf.popleft()
                    continue
                if self._pos < start:
                    gap = min(n - filled, start - self._pos)  # 块间静音：时间轴上真实存在
                    self._pos += gap
                    filled += gap
                    self.silence_samples += gap
                    continue
                off = self._pos - start
                take = min(n - filled, end - self._pos)
                out[filled:filled + take] = arr[off:off + take]
                self._pos += take
                filled += take
            if filled:
                self._has_audio = True
                if self._t_first_audio is None:
                    self._t_first_audio = time.perf_counter()
                    first = True
        if first:
            self._emit('first_audio', self.task_id)
        return out, filled

    # ---------------------------- 缓冲 ----------------------------

    def buffered_s(self):
        with self._lock:
            pos = self._pos
            total = 0
            for start, arr, _ in self._buf:
                total += max(0, start + arr.shape[0] - max(start, pos))
            return round(total / self.sr, 3)

    def _cur_index(self):
        with self._lock:
            if not self._ranges:
                return 0
            t = self._pos / self.sr
            idx = self._ranges[0][0]
            for i, start, _dur in self._ranges:
                if t >= start:
                    idx = i
                else:
                    break
            return idx

    def _finished(self):
        with self._lock:
            at_end = (self._end_samples > 0 and self._pos >= self._end_samples)
            return at_end or (
                not self._buf and self._task is not None
                and self._fetch is not None and not self._fetch.is_alive()
                and self._plan and len(self._ranges) >= len(self._plan))

    # ---------------------------- 拉块线程 ----------------------------

    def _start_fetch(self, gen, start_i):
        self._fetch = threading.Thread(target=self._fetch_loop, args=(gen, start_i),
                                       daemon=True, name='tts-fetch')
        self._fetch.start()

    def _fetch_loop(self, gen, i):
        task = self._task
        tid = task['task_id']
        fails = 0
        while True:
            with self._lock:
                if gen != self._gen or self._closing:
                    return
                plan_len = len(self._plan)
                enough = self.buffered_s() >= max(self.prefetch_s, self.buffer_high_s)
            if i >= plan_len:
                return
            if enough:
                time.sleep(0.02)
                continue
            try:
                b = self.client.block(tid, i)
                fails = 0
            except Exception as e:
                fails += 1
                log.warning('取块 %d 失败(%d/3): %s', i, fails, e)
                if fails >= 3:
                    self._emit('error', f'取块失败: {e}')
                    return
                time.sleep(0.3)
                continue
            with self._lock:
                if gen != self._gen:
                    return
                self._buf.append((int(round(b.offset_s * self.sr)), b.audio, b.index))
                self._ranges.append((b.index, b.offset_s, b.dur_s))
                self._end_samples = max(self._end_samples,
                                        int(round((b.offset_s + b.dur_s) * self.sr)))
            if b.index == 0:
                self.first_block_at = time.perf_counter()
                self._plan_gate(b)
            self._emit('block', b)
            i += 1

    # ---------------------------- 起播门槛 ----------------------------

    def _plan_gate(self, first_block):
        """用首块的实测合成倍率估「第二块什么时候回来」，决定要不要先攒缓冲。

        纯算术：首块落地就开播，此时手上只有 dur0；第二块要 gen(1) 秒才回来，
        若 gen(1) > dur0，中间就是必然的空档。门槛 = 这段缺口（上限 gate_max_s）。
        缓存命中的块不设门槛 —— 它本来就是毫秒级，等反而是白等。
        """
        if first_block.hit:
            return
        dur0 = first_block.dur_s or 0.0
        nxt_chars = len(self._plan[1]) if len(self._plan) > 1 else 0
        per_char = None
        if first_block.gen_s and first_block.chars:
            per_char = first_block.gen_s / first_block.chars
        elif dur0 and first_block.chars:
            per_char = dur0 / first_block.chars / 2.65  # 兜底：实测合成倍率
        self._per_char = per_char
        if not nxt_chars or not per_char:
            return
        gap = per_char * nxt_chars - dur0 + 0.25
        if gap <= 0:
            return
        gate = min(gap, self.gate_max_s)
        with self._lock:
            self._gate_s = gate
            self._gate_deadline = time.perf_counter() + gate + 3.0
        self.gate_waits.append(round(gate, 2))
        log.info('预计第二块晚 %.2fs 回来 → 起播门槛 %.2fs', gap, gate)
        self._emit('gate', gate)

    # ---------------------------- 进度上报 ----------------------------

    def _ensure_reporter(self, gen):
        # 每次都新起一个：换挡/跳句会换代，旧线程在下一次循环处自己退出
        self._reporter = threading.Thread(target=self._report_loop, args=(gen,),
                                          daemon=True, name='tts-report')
        self._reporter.start()

    def _report_loop(self, gen):
        last = None
        while True:
            with self._lock:
                if gen != self._gen or self._closing or self._task is None:
                    return
                task_id = self._task['task_id']
                pos = round(self._pos / self.sr, 3)
                idx = self._cur_index()
                finished = self._finished()
                played = round((self._pos - self._init_pos) / self.sr, 3)
            if last != (idx, pos):
                try:
                    self.client.progress(task_id, idx, pos, played)
                    last = (idx, pos)
                except Exception:
                    pass
            if finished and pos > 0:
                with self._lock:
                    if gen == self._gen:
                        self._done = True
                self._emit('done', task_id)
                return
            time.sleep(0.5)

    # ---------------------------- 对外操作 ----------------------------

    def _emit(self, name, payload=None):
        if self.on_event:
            try:
                self.on_event(name, payload)
            except Exception as e:
                log.warning('事件回调失败 %s: %s', name, e)

    def _ensure_sink(self):
        if self.sink is not None and not self.sink.active:
            try:
                self.sink.start()
            except Exception as e:
                log.error('打开音频设备失败: %s', e)
                raise

    def attach(self, task_id, chunk_idx=0, pos_s=0.0, plan=None, text=None, voice=None,
               rate=None, chunks=None):
        """把播放位置移到某个任务的某一点，并接着播（不重启音频流）"""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._buf.clear()
            self._ranges.clear()
            n_chunks = chunks or (len(plan) if plan else 0)
            self._task = dict(task_id=task_id, text=text, voice=voice,
                              rate=float(rate or config.RATE_DEFAULT), chunks=n_chunks)
            self._plan = list(plan) if plan else [''] * n_chunks
            self._pos = int(round(float(pos_s or 0) * self.sr))
            self._init_pos = self._pos
            self._paused = False
            self._has_audio = False
            self._done = n_chunks == 0
            self._t_attach = time.perf_counter()
            self._t_first_audio = None
            self._gate_s = 0.0
            self._per_char = None
            self._end_samples = 0
            self.underruns = 0
            self.silence_samples = 0
            self.first_block_at = None
        self._ensure_sink()
        self._start_fetch(gen, int(chunk_idx))
        if n_chunks:
            self._ensure_reporter(gen)
        return gen

    def speak(self, text, voice=None, rate=None, if_same='ignore'):
        """新选中就新任务；同一段文字正在播时不重启（防手抖连按两次从头念）"""
        text = (text or '').strip()
        if not text:
            return dict(ok=False, reason='empty')
        cur = self._task
        if if_same == 'ignore' and cur and not self._done and (cur.get('text') or '') == text:
            return dict(ok=True, ignored='same_text', task_id=cur['task_id'], pos_s=self.pos_s)
        t0 = time.perf_counter()
        info = self.client.speak(text, voice, rate)
        plan = [c['text'] for c in info['plan']]
        self.attach(info['task_id'], 0, 0.0, plan=plan, text=text,
                    voice=info['voice'], rate=info['rate'], chunks=info['chunks'])
        return dict(ok=True, task_id=info['task_id'], chunks=info['chunks'],
                    chars=info['chars'], rate=info['rate'],
                    cached_blocks=info.get('cached_blocks'),
                    speak_ms=round((time.perf_counter() - t0) * 1000, 1))

    def time_to_first_audio(self):
        """从 attach 到第一个采样真正出声（秒）；没出声返回 None"""
        with self._lock:
            if self._t_first_audio is None:
                return None
            return round(self._t_first_audio - self._t_attach, 3)

    def pause(self):
        with self._lock:
            if self._task is None:
                return False
            self._paused = True
        self._emit('pause', self.task_id)
        return True

    def resume(self):
        with self._lock:
            if self._task is None:
                return False
            self._paused = False
            self._done = False
        self._ensure_sink()
        self._emit('resume', self.task_id)
        return True

    def toggle(self):
        return self.resume() if self._paused else self.pause()

    def seek(self, chunk_idx=None, pos_s=None):
        with self._lock:
            if self._task is None:
                return None
            task = dict(self._task)
            plan, chunks = list(self._plan), len(self._plan)
        loc = self.client.locate(task['task_id'], chunk_idx, pos_s)
        self.attach(task['task_id'], loc['chunk_idx'], loc['pos_s'], plan=plan,
                    text=task['text'], voice=task['voice'], rate=task['rate'], chunks=chunks)
        return loc

    def next_sentence(self):
        idx = self._cur_index()
        if self._plan and idx + 1 >= len(self._plan):
            return dict(ok=False, reason='last')
        return self.seek(chunk_idx=idx + 1)

    def prev_sentence(self):
        with self._lock:
            idx = self._cur_index()
            start = next((s for i, s, _ in self._ranges if i == idx), 0.0)
            here = self._pos / self.sr
        if here - start > 0.8:  # 已经念进去一段了：先回本句开头
            return self.seek(chunk_idx=idx)
        if idx == 0:
            return dict(ok=False, reason='first')
        return self.seek(chunk_idx=idx - 1)

    def set_rate(self, rate):
        """切倍速：先起「剩余文本新任务」（毫秒级），再后台合成首块无缝换挡。

        期间**旧速率继续播**，所以用户听不到等待。换挡点按块内比例映射
        （同一个块在同一句话的同一处），不会退回去重听。
        """
        with self._lock:
            if self._task is None:
                return dict(ok=False, reason='no_task')
            task = dict(self._task)
            idx = self._cur_index()
            pos = self._pos / self.sr
            gen = self._gen
            start = next((s for i, s, _ in self._ranges if i == idx), 0.0)
            dur = next((d for i, _s, d in self._ranges if i == idx), 0.0)
            ratio = min(max((pos - start) / dur, 0.0), 1.0) if dur else 0.0
        if abs(rate - task['rate']) < 1e-6:
            return dict(ok=True, unchanged=True, rate=rate)

        t0 = time.perf_counter()
        r = self.client.rate(task['task_id'], rate, idx)
        queued_ms = round((time.perf_counter() - t0) * 1000, 1)
        plan = [c['text'] for c in r['plan']]
        th = threading.Thread(target=self._swap_rate,
                              args=(gen, r, plan, task, rate, idx, ratio, t0),
                              daemon=True, name='tts-rate')
        th.start()
        return dict(ok=True, task_id=r['task_id'], rate=rate, queued_ms=queued_ms,
                    async_=True, from_chunk=idx, tail_chars=r.get('tail_chars'))

    def _swap_rate(self, gen, r, plan, old_task, rate, old_idx, ratio0, t0):
        """先把新速率的音频攒够再换挡。

        判据不是「攒了多少秒」，而是**换挡点之后还剩多少秒**：换挡点若落在块尾
        （比例接近 1），攒了 3 秒也只剩零点几秒可播，下一块还没合成好就静音了
        （实测 1.26s 断流）。所以按 (1-比例)*首块 + 后续块 算「跑道」，
        跑道够 1.5s 就换；旧音频快播完了（再等就真断）也提前换。
        """
        need = max(1.5, self.prefetch_s * 0.5)
        blocks, buf_s, ratio = [], 0.0, ratio0
        try:
            i = 0
            while i < len(plan) and i < 4:
                blk = self.client.block(r['task_id'], i)  # 新速率逐块合成
                blocks.append(blk)
                buf_s += blk.dur_s
                with self._lock:
                    if gen != self._gen:
                        return
                    old_left = self.buffered_s()
                    pos = self._pos / self.sr
                    start = next((s for j, s, _ in self._ranges if j == old_idx), 0.0)
                    dur = next((d for j, _s, d in self._ranges if j == old_idx), 0.0)
                    ratio = min(max((pos - start) / dur, 0.0), 1.0) if dur else ratio0
                runway = (1 - ratio) * blocks[0].dur_s + sum(b.dur_s for b in blocks[1:])
                if runway >= need or old_left < 0.6:
                    break
                i += 1
        except Exception as e:
            log.warning('换挡合成失败，继续用旧速率: %s', e)
            self._emit('rate_failed', str(e))
            return
        with self._lock:
            if gen != self._gen or not self._task or self._task['task_id'] != old_task['task_id']:
                log.info('换挡放弃：期间又触发了新的朗读')
                return
            # 合成这段时间旧音频还在往前走，按换挡瞬间的位置重新算比例
            pos = self._pos / self.sr
            start = next((s for i, s, _ in self._ranges if i == old_idx), 0.0)
            dur = next((d for i, _s, d in self._ranges if i == old_idx), 0.0)
            ratio = min(max((pos - start) / dur, 0.0), 1.0) if dur else ratio0
        new_pos = round(blocks[0].offset_s + ratio * blocks[0].dur_s, 3)
        ok = self._swap_in(gen, r['task_id'], plan, blocks, new_pos, rate,
                           text=old_task.get('text'), voice=old_task.get('voice'))
        took = round(time.perf_counter() - t0, 3)
        if ok:
            log.info('换挡完成 %.2f× → %.2f×（缓冲 %.1fs，耗时 %.3fs）',
                     old_task['rate'], rate, buf_s, took)
            self._emit('rate', dict(rate=rate, took_s=took, chunk_idx=blocks[0].index,
                                    pos_s=new_pos))
            self.last_rate_swap = dict(took_s=took, rate=rate, pos_s=new_pos,
                                       buffered_s=round(buf_s, 2))

    def _swap_in(self, gen, task_id, plan, blocks, pos_s, rate, text=None, voice=None):
        """把当前任务原子替换成新任务，并把已合成好的块直接塞进缓冲（不静音）"""
        first = blocks[0]
        with self._lock:
            if gen != self._gen:
                return False
            self._gen += 1
            new_gen = self._gen
            self._buf.clear()
            self._ranges.clear()
            self._task = dict(task_id=task_id, text=text, voice=voice, rate=float(rate),
                              chunks=len(plan))
            self._plan = list(plan)
            self._pos = int(round(pos_s * self.sr))
            self._init_pos = self._pos
            self._has_audio = True  # 换挡不是从零开始，别把这一瞬间算成断流
            self._done = False
            self._end_samples = 0
            for blk in blocks:
                self._buf.append((int(round(blk.offset_s * self.sr)), blk.audio, blk.index))
                self._ranges.append((blk.index, blk.offset_s, blk.dur_s))
                self._end_samples = max(self._end_samples,
                                        int(round((blk.offset_s + blk.dur_s) * self.sr)))
        self._start_fetch(new_gen, first.index + len(blocks))
        self._ensure_reporter(new_gen)
        return True

    def resume_last(self):
        """重启后接着听：拿最近一个任务，从它记录的块内偏移续"""
        items = self.client.tasks(1)
        if not items:
            return dict(ok=False, reason='no_task')
        t = items[0]
        loc = self.client.resume(t['id'], t.get('chunk_idx'), t.get('pos_s'))
        self.attach(t['id'], loc['chunk_idx'], loc['pos_s'], plan=None,
                    text=None, voice=t.get('voice'), rate=t.get('rate'), chunks=t['chunks'])
        return dict(ok=True, task_id=t['id'], chunk_idx=loc['chunk_idx'], pos_s=loc['pos_s'])

    def stop(self, notify_server=True):
        """停止播放（服务端保留音频，回头还能接着听）"""
        with self._lock:
            task = dict(self._task) if self._task else None
            pos, idx = self._pos / self.sr, self._cur_index()
            self._gen += 1
            self._task = None
            self._plan = []
            self._buf.clear()
            self._ranges.clear()
            self._done = True
            self._paused = False
        if task and notify_server:
            try:
                self.client.stop(task['task_id'], idx, pos)
            except Exception:
                pass
        self._emit('stop', task['task_id'] if task else None)
        return bool(task)

    def status(self):
        with self._lock:
            task = dict(self._task) if self._task else None
            return dict(
                task_id=task['task_id'] if task else None,
                text_head=(task.get('text') or '')[:20] if task else '',
                playing=self.playing, paused=self._paused, done=self._done,
                rate=task['rate'] if task else config.RATE_DEFAULT,
                pos_s=round(self._pos / self.sr, 2),
                chunk_idx=self._cur_index() if task else 0,
                chunks=len(self._plan), buffered_s=self.buffered_s(),
                total_s=self._ranges[-1][1] + self._ranges[-1][2] if self._ranges else 0.0,
                underruns=self.underruns,
                underrun_events=list(self.underrun_events[-8:]),
                gate_waits=list(self.gate_waits),
                first_audio_s=self.time_to_first_audio(),
                sink=self.sink.kind if self.sink else None,
            )

    def wait_until(self, pred, timeout=30, interval=0.01):
        """测试/脚本用：等某个状态成立"""
        end = time.time() + timeout
        while time.time() < end:
            if pred(self):
                return True
            time.sleep(interval)
        return False

    def close(self):
        with self._lock:
            self._closing = True
            self._gen += 1
        if self.sink is not None:
            self.sink.stop()
