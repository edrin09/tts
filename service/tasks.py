"""任务：一次朗读 = 一个 task_id + 一串块。

关键约定
--------
* **块边界 = 句子边界**（`chunker.split` 保证），所以「上一句 / 下一句」等于
  「上一块 / 下一块」，不需要额外的分句器。
* **时间轴是绝对的**：每块有 `offset_s`（在整段音频里的起点，含块前 50ms 静音）
  和 `dur_s`（自身时长）。播放器只认这两个数，因此断点续播 = 记住 pos_s 再
  按 offset 找块，不需要「第几块第几秒」这种双重坐标。
* **块必须严格顺序生成**：块 k 的 offset 依赖块 0..k-1 的时长。代价是
  跳转到远处要顺带生成中间的块；收益是时间轴永远自洽。
* 状态落盘 `state.json`（原子替换），进程重启后还能接着听。
"""
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from core import chunker, config

log = logging.getLogger('tts.tasks')


class Task:
    __slots__ = ('id', 'text', 'voice', 'rate', 'plan', 'dir', 'blocks', 'status',
                 'chunk_idx', 'pos_s', 'played_s', 'created', 'lock', 'gen_lock',
                 'gap_s', 'stopped', '_save_at')

    def __init__(self, tid, text, voice, rate, plan, root, gap_s):
        self.id = tid
        self.text = text
        self.voice = voice
        self.rate = rate
        self.plan = plan
        self.dir = Path(root) / 'task' / tid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blocks = []
        self.status = 'ready'
        self.chunk_idx = 0
        self.pos_s = 0.0
        self.played_s = 0.0
        self.created = time.time()
        self.lock = threading.RLock()
        self.gen_lock = threading.Lock()
        self.gap_s = gap_s
        self.stopped = False
        self._save_at = 0.0

    # ---------------------------- 视图 ----------------------------

    @property
    def done(self):
        return len(self.blocks) >= len(self.plan)

    def total_s(self):
        if not self.blocks:
            return 0.0
        b = self.blocks[-1]
        return round(b['offset_s'] + b['dur_s'], 3)

    def dur_of(self, i):
        return self.blocks[i]['dur_s'] if i < len(self.blocks) else None

    def block_path(self, i):
        return self.dir / f'{i}.wav'

    def to_dict(self, with_plan=True):
        d = dict(id=self.id, voice=self.voice, rate=self.rate, chars=len(self.text),
                 chunks=len(self.plan), generated=len(self.blocks),
                 status=self.status, chunk_idx=self.chunk_idx,
                 pos_s=round(self.pos_s, 3), played_s=round(self.played_s, 3),
                 total_s=self.total_s(), created=self.created,
                 text_head=self.text[:40], done=self.done)
        if with_plan:
            d['plan'] = [dict(i=i, text=t, chars=len(t)) for i, t in enumerate(self.plan)]
            d['blocks'] = [dict(i=b['i'], offset_s=b['offset_s'], dur_s=b['dur_s'],
                                chars=b['chars'], hit=b.get('hit'))
                           for b in self.blocks]
        return d


class TaskStore:
    def __init__(self, engine, cache, root, keep=20, gap_ms=None):
        self.engine = engine
        self.cache = cache
        self.root = Path(root)
        (self.root / 'task').mkdir(parents=True, exist_ok=True)
        self.keep = keep
        # 块间留白按引擎取（config.GAP_MS：本地 50ms / 讯飞 300ms，见 .env 注释）
        self.gap_s = (config.GAP_MS if gap_ms is None else gap_ms) / 1000.0
        self.tasks = {}
        self.lock = threading.Lock()
        self.synth_lock = threading.Lock()  # 模型同一时刻只跑一次前向
        self.synth_count = 0
        self._prune_lock = threading.Lock()
        self._last_prune = 0.0

    # ---------------------------- 创建 / 载入 ----------------------------

    def create(self, text, voice=None, rate=None):
        text = (text or '').strip()
        if not text:
            raise ValueError('text 为空')
        plan = self.engine.plan(text)
        if not plan:
            raise ValueError('切块后为空')
        tid = uuid.uuid4().hex[:16]
        # 音色名按引擎归一：本地落成 zf_001/zm_029，讯飞保留 female/male 交给引擎映射
        t = Task(tid, text, config.resolve_voice(voice), float(rate or config.RATE_DEFAULT),
                 plan, self.root, self.gap_s)
        with self.lock:
            self.tasks[tid] = t
        self._save(t)
        self.prune_async()   # 删旧任务是重活（rmtree 带 wav，实测 230ms），不能放在 /speak 的同步路径上
        return t

    def get(self, tid):
        if not tid:
            return None  # 缺参数时别拼路径（Path / None 会 TypeError）
        with self.lock:
            t = self.tasks.get(tid)
        if t:
            return t
        return self._load(tid)

    def _load(self, tid):
        if not tid:
            return None
        f = self.root / 'task' / tid / 'state.json'
        if not f.is_file():
            return None
        try:
            d = json.loads(f.read_text('utf-8'))
        except (OSError, ValueError):
            return None
        plan = [b['text'] for b in d.get('plan', [])]
        if not plan:
            return None
        t = Task(d['id'], d['text'], d.get('voice'), float(d.get('rate') or 1.0),
                 plan, self.root, self.gap_s)
        t.blocks = [dict(b) for b in d.get('blocks', [])]
        t.status = d.get('status', 'ready')
        t.chunk_idx = int(d.get('chunk_idx') or 0)
        t.pos_s = float(d.get('pos_s') or 0.0)
        t.played_s = float(d.get('played_s') or 0.0)
        t.created = d.get('created', time.time())
        # 块文件丢了就截断到最后一个连续存在的块，保证时间轴自洽
        keep = 0
        for i in range(len(t.blocks)):
            if t.block_path(i).is_file():
                keep = i + 1
            else:
                break
        t.blocks = t.blocks[:keep]
        if t.status == 'generating':
            t.status = 'ready'
        with self.lock:
            self.tasks[tid] = t
        return t

    # ---------------------------- 生成 ----------------------------

    def ensure(self, task, i):
        """确保块 i 已生成（含它前面的所有块），返回该块元数据"""
        if i < 0:
            i = 0
        if i >= len(task.plan):
            raise IndexError(f'块号 {i} 超出范围（共 {len(task.plan)} 块）')
        with task.gen_lock:
            while len(task.blocks) <= i and not task.stopped:
                k = len(task.blocks)
                meta = self._gen(task, k)
                with task.lock:
                    task.blocks.append(meta)
                self._save(task)
        if i >= len(task.blocks):
            raise RuntimeError('任务已停止，未能生成目标块')
        return task.blocks[i]

    def _gen(self, task, k):
        text = task.plan[k]
        key = self.cache.key(text, task.voice, task.rate)
        meta = self.cache.get(key)
        hit = meta is not None
        if not hit:
            with self.synth_lock:  # 模型串行：多任务并发时也不抢 CPU
                meta = self.cache.get(key)  # 等锁期间别人可能刚做完
                hit = meta is not None
                if not hit:
                    t0 = time.perf_counter()
                    audio, info = self.engine.block(text, task.voice, task.rate)
                    self.synth_count += 1
                    meta = self.cache.put(
                        key, audio, self.engine.sr,
                        dict(text=text, voice=task.voice, rate=task.rate, chars=len(text),
                             dur_s=round(len(audio) / self.engine.sr, 4),
                             phonemes=info['phonemes'], speed=info['speed'],
                             gen_s=round(time.perf_counter() - t0, 3)))
        # 任务自己留一份（硬链接，同卷时不额外占空间；缓存被淘汰也不影响续播）
        dst = task.block_path(k)
        if not dst.is_file():
            try:
                os.link(meta['wav_path'], dst)
            except OSError:
                shutil.copy2(meta['wav_path'], dst)
        prev = task.blocks[-1] if task.blocks else None
        offset = 0.0 if prev is None else prev['offset_s'] + prev['dur_s'] + self.gap_s
        return dict(i=k, text=text, chars=len(text), dur_s=round(float(meta['dur_s']), 4),
                    offset_s=round(offset, 4), key=key, hit=hit,
                    phonemes=meta.get('phonemes'), gen_s=meta.get('gen_s'),
                    speed=meta.get('speed'))

    # ---------------------------- 定位 / 进度 ----------------------------

    def locate(self, task, chunk_idx=None, pos_s=None):
        """把「块号 / 秒」解析成绝对时间轴上的位置，顺带把需要的块生成出来"""
        if pos_s is not None:
            pos_s = max(0.0, float(pos_s))
            # 从后往前找包含该时刻的块
            i = len(task.blocks) - 1
            while i >= 0 and task.blocks[i]['offset_s'] > pos_s:
                i -= 1
            i = max(0, i)
        else:
            i = max(0, int(chunk_idx or 0))
            i = min(i, len(task.plan) - 1)
        meta = self.ensure(task, i)
        if pos_s is None:
            pos_s = meta['offset_s']
        # 落在块末尾之后的空白里时，直接算作下一块起点
        if pos_s > meta['offset_s'] + meta['dur_s'] + self.gap_s:
            nxt = min(i + 1, len(task.plan) - 1)
            if nxt != i:
                meta = self.ensure(task, nxt)
                pos_s = meta['offset_s']
        return dict(chunk_idx=meta['i'], offset_s=meta['offset_s'], dur_s=meta['dur_s'],
                    pos_s=round(pos_s, 4), total_s=task.total_s(), done=task.done)

    def progress(self, tid, chunk_idx=None, pos_s=None, played_s=None):
        t = self.get(tid)
        if not t:
            return None
        with t.lock:
            if chunk_idx is not None:
                t.chunk_idx = int(chunk_idx)
            if pos_s is not None:
                t.pos_s = round(float(pos_s), 3)
            if played_s is not None:
                t.played_s = round(float(played_s), 3)
        self._save(t, force=True)
        return dict(chunk_idx=t.chunk_idx, pos_s=t.pos_s, played_s=t.played_s)

    # ---------------------------- 落盘 / 清理 ----------------------------

    def _save(self, task, force=False):
        now = time.time()
        if not force and now - task._save_at < 0.33:
            return
        task._save_at = now
        with task.lock:
            d = task.to_dict()
            d['text'] = task.text
        f = task.dir / 'state.json'
        tmp = task.dir / 'state.json.tmp'
        try:
            tmp.write_text(json.dumps(d, ensure_ascii=False), 'utf-8')
            os.replace(tmp, f)
        except OSError:
            pass

    def prune_async(self, min_interval=10.0):
        """后台清理旧任务（10s 节流）。清理不能放进 /speak 的同步路径：
        rmtree 一次 200ms+，会把首字预算直接吃掉。"""
        now = time.time()
        if now - self._last_prune < min_interval:
            return
        if not self._prune_lock.acquire(blocking=False):
            return
        self._last_prune = now

        def run():
            try:
                n = self.prune()
                if n:
                    log.info('清理旧任务 %d 个', n)
            except Exception as e:
                log.warning('清理任务失败: %s', e)
            finally:
                self._prune_lock.release()

        threading.Thread(target=run, daemon=True, name='tts-prune').start()

    def prune(self):
        """只留最近 keep 个任务（按 state.json 修改时间）"""
        dirs = []
        for d in (self.root / 'task').iterdir():
            if not d.is_dir():
                continue
            f = d / 'state.json'
            try:
                dirs.append((f.stat().st_mtime if f.is_file() else 0, d))
            except OSError:
                continue
        if len(dirs) <= self.keep:
            return 0
        removed = 0
        for _, d in sorted(dirs)[:len(dirs) - self.keep]:
            if d.name in self.tasks:
                continue
            try:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            except OSError:
                pass
        return removed

    def recent(self, limit=8):
        out = []
        for d in (self.root / 'task').iterdir():
            f = d / 'state.json'
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((st.st_mtime, d))
        out.sort(reverse=True)
        items = []
        for _, d in out[:limit]:
            t = self.tasks.get(d.name) or self._load(d.name)
            if t:
                items.append(t.to_dict(with_plan=False))
        return items

    def stats(self):
        with self.lock:
            n = len(self.tasks)
        return dict(tasks=n, synth_count=self.synth_count)
