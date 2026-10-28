"""服务客户端：把 HTTP 接口包成方法，播放器只跟它打交道。

* 每个线程一个 requests.Session（块请求会长时间阻塞，共用一个 session 容易踩
  连接复用的竞态），本地回环建连成本可忽略
* `block()` 返回解码好的 float32 单声道波形 + 该块在整段里的绝对位置
"""
import io
import threading

import numpy as np
import requests
import soundfile as sf

from core import config


class Block:
    __slots__ = ('index', 'audio', 'offset_s', 'dur_s', 'chars', 'hit', 'total_s',
                 'chunks', 'rate', 'gen_s', 'fetched_at')

    def __init__(self, index, audio, offset_s, dur_s, chars, hit, total_s, chunks, rate,
                 gen_s=0.0):
        self.index = index
        self.audio = audio
        self.offset_s = offset_s
        self.dur_s = dur_s
        self.chars = chars
        self.hit = hit
        self.total_s = total_s
        self.chunks = chunks
        self.rate = rate
        self.gen_s = gen_s
        self.fetched_at = 0.0

    def __repr__(self):
        return (f'<Block {self.index} {self.offset_s:+.2f}s {self.dur_s:.2f}s '
                f'{len(self.audio)}smp{" hit" if self.hit else ""}>')


class Client:
    def __init__(self, host=None, port=None, timeout=180):
        self.base = f'http://{host or config.HOST}:{port or config.PORT}'
        self.timeout = timeout
        self._local = threading.local()

    def _s(self):
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
            s.headers.update({'Connection': 'keep-alive'})
            self._local.session = s
        return s

    # ---------------------------- 基础 ----------------------------

    def health(self):
        return self._s().get(f'{self.base}/health', timeout=5).json()

    def alive(self):
        try:
            return bool(self.health().get('loaded'))
        except Exception:
            return False

    # ---------------------------- 朗读 ----------------------------

    def speak(self, text, voice=None, rate=None):
        body = {'text': text}
        if voice:
            body['voice'] = voice
        if rate:
            body['rate'] = float(rate)
        r = self._s().post(f'{self.base}/speak', json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def block(self, task_id, i):
        r = self._s().get(f'{self.base}/block', params={'task_id': task_id, 'i': i},
                          timeout=self.timeout)
        r.raise_for_status()
        audio, sr = sf.read(io.BytesIO(r.content), dtype='float32', always_2d=False)
        if audio.ndim > 1:
            audio = audio[:, 0]
        h = r.headers
        return Block(index=int(h.get('X-Index', i)), audio=np.ascontiguousarray(audio),
                     offset_s=float(h.get('X-Offset-S', 0)), dur_s=float(h.get('X-Dur-S', 0)),
                     chars=int(h.get('X-Chars', 0)), hit=h.get('X-Cache-Hit') == '1',
                     total_s=float(h.get('X-Total-S', 0) or 0),
                     chunks=int(h.get('X-Chunks', 0) or 0), rate=float(h.get('X-Rate', 0) or 0),
                     gen_s=float(h.get('X-Gen-S', 0) or 0))

    # ---------------------------- 状态 / 续播 ----------------------------

    def state(self, task_id):
        r = self._s().get(f'{self.base}/state', params={'task_id': task_id}, timeout=10)
        r.raise_for_status()
        return r.json()

    def locate(self, task_id, chunk_idx=None, pos_s=None):
        p = {'task_id': task_id}
        if chunk_idx is not None:
            p['chunk_idx'] = int(chunk_idx)
        if pos_s is not None:
            p['pos_s'] = float(pos_s)
        r = self._s().get(f'{self.base}/locate', params=p, timeout=120)
        r.raise_for_status()
        return r.json()

    def resume(self, task_id, chunk_idx=None, pos_s=None):
        body = {'task_id': task_id}
        if chunk_idx is not None:
            body['chunk_idx'] = int(chunk_idx)
        if pos_s is not None:
            body['pos_s'] = float(pos_s)
        r = self._s().post(f'{self.base}/resume', json=body, timeout=180)
        r.raise_for_status()
        return r.json()

    def progress(self, task_id, chunk_idx=None, pos_s=None, played_s=None):
        body = {'task_id': task_id}
        if chunk_idx is not None:
            body['chunk_idx'] = int(chunk_idx)
        if pos_s is not None:
            body['pos_s'] = float(pos_s)
        if played_s is not None:
            body['played_s'] = float(played_s)
        r = self._s().post(f'{self.base}/state', json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def rate(self, task_id, rate, chunk_idx=None):
        """换倍速：服务端只切块不合成（合成由播放器在后台补），所以很快返回"""
        body = {'task_id': task_id, 'rate': float(rate)}
        if chunk_idx is not None:
            body['chunk_idx'] = int(chunk_idx)
        r = self._s().post(f'{self.base}/rate', json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def stop(self, task_id, chunk_idx=None, pos_s=None):
        body = {'task_id': task_id}
        if chunk_idx is not None:
            body['chunk_idx'] = int(chunk_idx)
        if pos_s is not None:
            body['pos_s'] = float(pos_s)
        r = self._s().post(f'{self.base}/stop', json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def tasks(self, limit=8):
        r = self._s().get(f'{self.base}/tasks', timeout=10)
        r.raise_for_status()
        return r.json().get('tasks', [])[:limit]

    def shutdown(self):
        try:
            return self._s().post(f'{self.base}/shutdown', timeout=5).json()
        except Exception:
            return None
