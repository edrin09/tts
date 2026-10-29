"""在线识别：讯飞语音听写（流式 WebAPI），以及 HTTP 兜底口子。

为什么用「流式会话」而不是原来那种「整段上传」
----------------------------------------------
听写 WS 是真的边传边出字：每 40ms 一帧音频送上去，服务端回 partial，
所以按住 Alt 说话时小窗能实时刷新 —— 这和本地 SenseVoice 靠「整段重解码」
模拟出来的 partial 是两种东西，前者不随录音变长而变慢。

两种协议（.env: ASR_XFYUN_PROTOCOL）
------------------------------------
* ws（默认）：wss://iat-api.xfyun.cn/v2/iat，帧序 status 0→1…→2，
  返回 JSON 里带 `pgs`（apd 追加 / rpl 替换区间），必须按 rg 处理，否则
  动态修正会把已经显示的字重复一遍。
* http：POST {ASR_XFYUN_HTTP_URL}，Bearer {ASR_XFYUN_HTTP_KEY}，
  body {"audio":"<wav base64>","sample_rate":16000}，响应 {"text":"..."}。
  给自建代理或别的兼容服务用；没有 partial，松手后才整段上传。
* "ws,http"：ws 连不上时自动退到 http（http 没配就等于纯 ws）。
"""
import base64
import io
import json
import logging
import threading
import wave

import numpy as np

from core import config, xfyun

log = logging.getLogger('tts.asr-xfyun')

FRAME_BYTES = 1280          # 40ms @ 16k / 16bit / 单声道，讯飞推荐帧长
OK = 0


def ready():
    return xfyun.asr_ready()


def protocols():
    return config.XFYUN_ASR_PROTOCOLS


def _to_pcm16(samples):
    """float32 → 16bit LE bytes。

    录音回调给的是 int16 量级的 float（sounddevice dtype='int16' 后再转
    float32，数值在 ±32768 而不是 ±1），所以先看量级再决定要不要归一化 ——
    直接 clip 到 ±1 会把整段削成方波。
    """
    a = np.asarray(samples, dtype=np.float32)
    if a.size == 0:
        return b''
    peak = float(np.max(np.abs(a)))
    if peak > 1.5:
        a = a / 32768.0
    return (np.clip(a, -1.0, 1.0) * 32767).astype('<i2').tobytes()


def samples_to_wav_bytes(samples, sr=16000):
    """float32 单声道 → 16bit PCM WAV 字节（http 协议上传用）。"""
    pcm = _to_pcm16(samples)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


class Session:
    """一次按住说话 = 一个会话：内部一条 WS + 一条收包线程。"""

    def __init__(self, url=None):
        import websocket

        self.appid, self.key, self.secret = xfyun.asr_creds()
        self.url = url or config.XFYUN_ASR_WS_URL
        self._lock = threading.Lock()
        self._text = ''
        self._sn = {}                 # sn → 该句文本（pgs=rpl 时按 rg 删除重建）
        self._buf = b''
        self._first = True
        self._err = None
        self._closed = False
        self._done = threading.Event()

        full = xfyun.auth_url(self.url, self.key, self.secret)
        # 单独一个较短的握手超时：这个值决定「按住 Alt 之后多久才出字」，
        # 用 TTS 的 20s 会让失败场景下小窗干等 20 秒
        self.ws = websocket.create_connection(
            full, timeout=config.XFYUN_ASR_CONNECT_TIMEOUT)
        self._t = threading.Thread(target=self._recv_loop, daemon=True,
                                   name='tts-iat-recv')
        self._t.start()

    # ---------------------------- 音频 ----------------------------

    def accept(self, samples):
        if self._closed:
            return
        self._buf += _to_pcm16(samples)
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            self._send(frame, 0 if self._first else 1)

    def partial(self):
        with self._lock:
            return self._text

    def finish(self, timeout=4.0):
        """送结束帧，等服务端把最后一句吐完（超时就拿现有的）。"""
        if self._closed:
            return self.partial()
        try:
            if self._first:      # 一个字都没说：先补一帧静音，否则服务端拒收结束帧
                self._send(b'\x00' * FRAME_BYTES, 0)
            self._send(b'', 2)
            self._done.wait(timeout)
        finally:
            self.close()
        if self._err and not self._text:
            raise RuntimeError(f'讯飞听写失败: {self._err}')
        return self.partial()

    def cancel(self):
        self.close()

    def close(self):
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass

    # ---------------------------- 协议 ----------------------------

    def _send(self, pcm, status):
        body = {'data': {'status': status,
                         'format': 'audio/L16;rate=16000',
                         'encoding': 'raw',
                         'audio': base64.b64encode(pcm).decode('ascii')}}
        if self._first:
            body['common'] = {'app_id': self.appid}
            body['business'] = {
                'language': 'zh_cn',
                'domain': 'iat',
                'accent': config.XFYUN_ASR_ACCENT,
                'vad_eos': 10000,      # 端点检测放宽到 10s：结束时机由「松开 Alt」决定
                'dwa': 'wpgs',         # 开启动态修正，边录边出字才顺
                'ptt': 1,              # 标点
            }
            self._first = False
        self.ws.send(json.dumps(body))

    def _recv_loop(self):
        try:
            while not self._closed:
                msg = json.loads(self.ws.recv())
                code = msg.get('code')
                if code != OK:
                    self._err = f"{code}: {msg.get('message')}"
                    log.warning('讯飞听写返回错误 %s', self._err)
                    break
                d = msg.get('data') or {}
                r = d.get('result')
                if r:
                    self._apply(r)
                if d.get('status') == 2:
                    break
        except Exception as e:
            if not self._closed:
                self._err = f'{type(e).__name__}: {e}'
                log.warning('讯飞听写收包失败: %s', e)
        finally:
            self._done.set()

    def _apply(self, r):
        words = ''.join((w.get('cw') or [{}])[0].get('w', '') for w in r.get('ws') or [])
        sn = r.get('sn')
        rg = r.get('rg')
        with self._lock:
            if r.get('pgs') == 'rpl' and rg:
                for k in [k for k in self._sn if rg[0] <= k <= rg[1]]:
                    del self._sn[k]
            self._sn[sn] = words
            self._text = ''.join(self._sn[k] for k in sorted(self._sn))


def open_session():
    """建 ws 会话；失败时按协议表决定是否可以退到 http（返回 None 表示走 http）。"""
    protos = protocols()
    if protos and protos[0] == 'ws':
        try:
            return Session()
        except Exception as e:
            if len(protos) > 1:
                log.warning('讯飞听写 ws 连不上（%s），退到 http', e)
                return None
            raise
    return None


def http_transcribe(samples, sr=16000, timeout=None):
    """http 兜底：整段 wav POST 过去，认 {"text": "..."}。"""
    import requests

    if not config.XFYUN_ASR_HTTP_URL:
        raise RuntimeError('ASR_XFYUN_HTTP_URL 没配')
    token = config.XFYUN_ASR_HTTP_KEY or config.XFYUN_API_PASSWORD or config.XFYUN_API_KEY
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    r = requests.post(config.XFYUN_ASR_HTTP_URL,
                      json={'audio': base64.b64encode(samples_to_wav_bytes(samples, sr)).decode('ascii'),
                            'sample_rate': sr},
                      headers=headers, timeout=timeout or config.XFYUN_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return (data.get('text') or (data.get('data') or {}).get('text') or '').strip()
