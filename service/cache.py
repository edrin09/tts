"""块级缓存：同文本 + 同音色 + 同倍速 → 直接命中磁盘 WAV，不再过模型。

为什么要块级而不是整段级
------------------------
整段缓存只对「一字不差重读同一篇」有用；块级缓存对「不同文章里重复的句子」
也有用，而且断点续播、切倍速都要按块取音频，缓存粒度跟播放粒度一致最省事。

键的构成（少一个都会串味）
--------------------------
    sha1(salt | voice | rate | 块文本)
* 不含 rate 会让 1.0× 的缓存被 1.5× 命中（ROADMAP 1.4 明确点名的坑）
* 不含 voice 会让男女声互相命中
* salt 用来一次性作废旧缓存（调音色/改切块策略后改 .env 里的 TTS_CACHE_SALT）

存储
----
`TTS_CACHE_DIR/blk/<key>.wav`（PCM_16，24k 单声道 ≈ 48KB/s）+ `<key>.json`。
任务目录用硬链接指向同一份文件（同卷 NTFS），所以淘汰缓存不会弄坏已存在的任务。
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf


class BlockCache:
    def __init__(self, root, max_mb=200, salt='v1'):
        self.dir = Path(root) / 'blk'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(16, int(max_mb)) * 1024 * 1024
        self.salt = salt
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self._last_evict = 0.0
        self.evicted = 0

    # ---------------------------- 键 ----------------------------

    def key(self, text, voice, rate):
        raw = f'{self.salt}|{voice}|{rate:.3f}|{text}'.encode('utf-8')
        return hashlib.sha1(raw).hexdigest()[:32]

    def path(self, key):
        return self.dir / f'{key}.wav'

    # ---------------------------- 读写 ----------------------------

    def get(self, key):
        """命中返回元数据（含 wav_path），未命中返回 None"""
        wav, meta = self.path(key), self.dir / f'{key}.json'
        if not (wav.is_file() and meta.is_file()):
            with self._lock:
                self.misses += 1
            return None
        try:
            m = json.loads(meta.read_text('utf-8'))
        except (OSError, ValueError):
            with self._lock:
                self.misses += 1
            return None
        if m.get('bytes') and m['bytes'] != wav.stat().st_size:
            with self._lock:
                self.misses += 1
            return None
        try:
            os.utime(wav, None)  # 只更新修改时间，避免真正的 atime 写入开销
        except OSError:
            pass
        with self._lock:
            self.hits += 1
        m.update(key=key, wav_path=str(wav), hit=True)
        return m

    def put(self, key, audio, sr, meta):
        """落盘一块音频，返回元数据（含 wav_path）"""
        wav, jf = self.path(key), self.dir / f'{key}.json'
        tmp = self.dir / f'.{key}.{os.getpid()}.tmp.wav'
        sf.write(str(tmp), np.asarray(audio, dtype=np.float32), sr, subtype='PCM_16')
        m = dict(meta)
        m.update(key=key, sr=sr, bytes=tmp.stat().st_size, created=time.time(),
                 wav_path=str(wav), hit=False)
        with self._lock:
            os.replace(tmp, wav)
            jtmp = self.dir / f'.{key}.{os.getpid()}.tmp.json'
            jtmp.write_text(json.dumps(m, ensure_ascii=False), 'utf-8')
            os.replace(jtmp, jf)
        self.evict()   # 有节流，见 evict()
        return m

    def touch(self, key):
        try:
            os.utime(self.path(key), None)
        except OSError:
            pass

    # ---------------------------- 淘汰 ----------------------------

    def usage(self):
        total = 0
        files = []
        for p in self.dir.glob('*.wav'):
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            files.append((st.st_mtime, p))
        return total, files

    def evict(self, force=False, min_interval=5.0):
        """超过上限就按最久未用淘汰，直到回落到 80%。

        每写一块都扫一遍目录（stat 每个 wav）在块多时就是纯浪费，还会挤占合成线程，
        所以默认 5 秒最多扫一次；真超限了 force=True 立刻扫。
        """
        now = time.time()
        if not force and now - self._last_evict < min_interval:
            return 0
        self._last_evict = now
        total, files = self.usage()
        if total <= self.max_bytes and not force:
            return 0
        target = int(self.max_bytes * 0.8)
        removed = 0
        for _, p in sorted(files):
            if total <= target:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                (self.dir / f'{p.stem}.json').unlink(missing_ok=True)
                total -= size
                removed += 1
            except OSError:
                continue
        self.evicted += removed
        return removed

    def stats(self):
        total, files = self.usage()
        with self._lock:
            hits, misses = self.hits, self.misses
        return dict(blocks=len(files), mb=round(total / 1048576, 1),
                    max_mb=round(self.max_bytes / 1048576, 1),
                    hits=hits, misses=misses,
                    hit_rate=round(hits / (hits + misses), 3) if hits + misses else None)

    def clear(self):
        removed = 0
        for p in list(self.dir.glob('*.wav')):
            try:
                p.unlink()
                (self.dir / f'{p.stem}.json').unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        self.evicted += removed
        return removed
