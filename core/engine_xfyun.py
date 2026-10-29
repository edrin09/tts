"""在线引擎：讯飞 WebAPI 语音合成（国内直连，ws 与 http 双协议）。

与本地 Engine 对外接口完全一致（plan / block / sr / threads / warmup），
TaskStore 无感知 —— 换引擎只是换掉 service.build() 里的那一行。

两种协议（.env: TTS_XFYUN_PROTOCOL）
------------------------------------
* ws（默认，官方 WebAPI）：wss://tts-api.xfyun.cn/v2/tts
  HMAC-SHA256 签名握手 → 一帧 JSON 送文本 → 收若干帧 base64 raw PCM。
  实测一次调用约 0.3~0.6s（含握手），比 Edge 稳，因为不出境。
* http：给「自己搭的代理 / 别的兼容服务 / 将来讯飞出 HTTP 接口」留的口子。
  POST {TTS_XFYUN_HTTP_URL}，Bearer {TTS_XFYUN_HTTP_KEY}，
  body {"text","voice","rate","app_id"}；响应可以是
    ① 音频字节（Content-Type: audio/*）；② JSON {"audio": "<base64>"}
    （也认 {"data":{"audio":...}} 和 {"url":"..."}）。
* "ws,http"：先走 ws，失败自动回落 http（http 没配就等于纯 ws）。

采样率
------
24k 是按发音人分开授权的（2026-10-29 实测）：
    支持 24k：x4_yezi / aisjiuxu / aisxping / aisjinger / aisbabyxu
    不支持  ：xiaoyan / xiaofeng —— 返回 code=0 但空音频，不报错
引擎对这种空音频自动回落 16k 并记住该音色（见 _ws），调用方无感；
src_sr 报实际源码率，_resample 再上采样到 config.SAMPLE_RATE，
播放器/缓存/时间轴一行都不用改。

单次长度
--------
官方限制单次 <8000 字节（约 2000 汉字）。切块后每块 ≤60 字，永远够；
但 plan() 之外直接调 block() 时仍会截断到 TTS_XFYUN_MAX_CHARS 兜底。
"""
import base64
import json
import logging
import time

import numpy as np

from . import config, xfyun
from .online_util import pack as _pack, resample as _resample, sentences as _sentences, \
    split_long as _split_long, tail_punct

log = logging.getLogger('tts.engine-xfyun')

# 托盘/浮窗可选的音色（免费基础发音人）。.env 的 TTS_XFYUN_VOICES 会覆盖它
DEFAULT_VOICES = [
    ('xiaoyan', '小燕 · 女'),
    ('aisjiuxu', '许久 · 男'),
    ('aisxping', '小萍 · 女'),
    ('aisjinger', '小婧 · 女'),
    ('aisbabyxu', '许小宝 · 童'),
]


class XfyunEngine:
    sr = config.SAMPLE_RATE     # 对外永远是 24k（不支持 24k 的音色内部上采样）
    src_sr = 24000              # 讯飞实际给的采样率，从 TTS_XFYUN_AUF 解析

    def __init__(self):
        self.threads = config.THREADS      # 没有线程池，但 /health 要报个数
        self.load_s = 0.0
        self.protocols = config.XFYUN_TTS_PROTOCOLS
        self.timeout = config.XFYUN_TIMEOUT
        self.src_sr = config.XFYUN_SRC_SR
        self._no_hires = set()      # 不支持 TTS_XFYUN_AUF 采样率的 vcn，回落 16k 后记住
        self._no_lic = set()        # 11200 未授权的 vcn，回落基础发音人后记住

    # ---------------------------- 生命周期 ----------------------------

    def load(self):
        """校验凭据 + 显式 import 一次 websocket-client（缺包要在启动日志里说人话）"""
        xfyun.tts_creds()
        if 'ws' in self.protocols:
            import websocket  # noqa: F401
        if 'http' in self.protocols:
            import requests  # noqa: F401
        return self

    def warmup(self, text=None, voice=None):
        """轻预热：主要目的是启动时验证一次连通性，失败不拖垮服务。

        顺带把切块链路（chunker → jieba 词典）也点着：词典首次加载实测 0.9s，
        不预热的话这笔钱会算在用户第一次 /speak 的头上（本地引擎走 synth，
        早就顺带点过了，这里要对齐）。
        """
        t = time.perf_counter()
        try:
            self.plan(text or config.WARMUP_TEXT)
        except Exception as e:
            log.warning('切块预热失败（不影响朗读）: %s', e)
        try:
            self.block(text or config.WARMUP_TEXT, voice)
            log.info('讯飞引擎预热完成 %.2fs（连通性 OK，协议=%s）',
                     time.perf_counter() - t, ','.join(self.protocols))
        except Exception as e:
            log.warning('讯飞引擎预热失败（断网/凭据错？朗读时会重试）: %s', e)
        return round(time.perf_counter() - t, 3)

    # ---------------------------- 切块 ----------------------------

    def plan(self, text, first_limit=None):
        """按句打包切块：块边界落在句号上，逗号不切开。

        和本地引擎的 chunker.split 是两套逻辑（本地为「首字最快」把句子切碎，
        在线为「听着像人」把句子攒起来）：
          * 首块至少一整句，最多攒到 TTS_XFYUN_FIRST_CHUNK_MAX 字 —— 一句
            「读书这件事，慢下来才有味道。」整体合成，才不会念成两段短句
          * 之后每块攒到 TTS_XFYUN_CHUNK_MAX 字（默认 80）
          * 只有单句本身超长时才按词边界硬切
        """
        text = (text or '').strip()
        if not text:
            return []
        nosplit = config.XFYUN_NOSPLIT_CHARS or config.NOSPLIT_CHARS
        if first_limit is None and nosplit and len(text) <= nosplit:
            return [text]
        limit = config.XFYUN_CHUNK_MAX
        first_max = config.XFYUN_FIRST_CHUNK_MAX if first_limit is None else first_limit

        sents = _sentences(text)
        head, i, n = [], 0, 0
        while i < len(sents):
            s = sents[i]
            if head and (n + len(s) > first_max or n >= first_max):
                break
            head.append(s)
            n += len(s)
            i += 1
        first = ''.join(head) or text
        blocks = _split_long(first, max(limit, len(first))) if len(first) > limit else [first]
        blocks.extend(_pack(sents[i:], limit))
        # 尾巴太短就别单独成块（尾块短 = 一次额外请求 + 一次语调重置，
        # 而且短块后面还会挂一段静音，听着像句子被吃掉后半截）
        if len(blocks) > 1 and len(blocks[-1]) <= first_max \
                and len(blocks[-2]) + len(blocks[-1]) <= limit:
            blocks[-2] += blocks[-1]
            blocks.pop()
        return [b for b in blocks if b]

    # ---------------------------- 音色 / 语速 ----------------------------

    @staticmethod
    def _voice(voice):
        """female/male → 配置里的 vcn；已经是 vcn 就直传。

        本地引擎留下的 zf_001 / zm_029 在讯飞侧不存在（传上去会报 11200），
        所以按前缀兜底成男女声，避免切引擎后第一次朗读就失败。
        """
        v = (voice or config.VOICE_DEFAULT).strip()
        if v == 'female' or v.startswith('zf_'):
            return config.XFYUN_VOICE_FEMALE
        if v == 'male' or v.startswith('zm_'):
            return config.XFYUN_VOICE_MALE
        return v

    @staticmethod
    def _speed(rate):
        """1.0× → TTS_XFYUN_SPEED_BASE（默认 50＝自然语速），范围 0~100。"""
        v = int(round(config.XFYUN_SPEED_BASE * float(rate)))
        return max(0, min(100, v))

    @staticmethod
    def _tail_punct(text):
        """块尾补句号的开关包一层（实际逻辑在 online_util.tail_punct，与 Edge 共用）"""
        return tail_punct(text, config.XFYUN_TAIL_PUNCT)

    # ---------------------------- 合成 ----------------------------

    def _synth(self, text, vcn, speed, rate):
        """跑一遍协议表（ws → 可选 http 回落），返回 (pcm, src_sr)。"""
        pcm, src_sr, err = None, self.src_sr, None
        order = list(self.protocols)
        for i, proto in enumerate(order):
            try:
                if proto == 'ws':
                    pcm, src_sr = self._ws(text, vcn, speed)
                else:
                    pcm, src_sr = self._http(text, vcn, rate), self.src_sr
                err = None
                break
            except Exception as e:
                err = e
                if i + 1 < len(order):
                    log.warning('讯飞 %s 协议失败，回落 %s：%s', proto, order[i + 1], e)
        if err is not None:
            raise RuntimeError(f'讯飞合成失败（协议 {"/".join(order)}）：'
                               f'{type(err).__name__}: {err}') from err
        if pcm is None or len(pcm) == 0:
            raise RuntimeError('讯飞合成失败：服务返回了空音频')
        return pcm, src_sr

    def block(self, text, voice=None, rate=1.0):
        """合成一个块（不再切分），返回 (float32 mono @sample_rate, info)"""
        t = time.perf_counter()
        text = text[:config.XFYUN_MAX_CHARS]
        vcn = self._voice(voice)
        speed = self._speed(rate)
        send = self._tail_punct(text)

        fallback = None
        if vcn in self._no_lic and config.XFYUN_FALLBACK_VOICE:
            fallback, vcn = config.XFYUN_FALLBACK_VOICE, config.XFYUN_FALLBACK_VOICE
        try:
            pcm, src_sr = self._synth(send, vcn, speed, rate)
        except RuntimeError as e:
            # 11200 licc failed = 这个 vcn 没授权（超拟人/特色发音人试用到期就是它）。
            # 与其让朗读直接失败，不如回落免费基础发音人，并记住别再试。
            fb = (config.XFYUN_FALLBACK_VOICE or '').strip()
            if '11200' not in str(e) or not fb or vcn == fb:
                raise
            log.warning('音色 %s 未授权（%s），本次回落 %s', vcn, str(e)[:80], fb)
            self._no_lic.add(vcn)
            fallback = fb
            pcm, src_sr = self._synth(send, fb, speed, rate)

        wav = _resample(pcm, src_sr, self.sr)
        info = dict(text=text, sent=send, chars=len(text), phonemes=0,
                    audio_s=round(len(wav) / self.sr, 3),
                    gen_s=round(time.perf_counter() - t, 3), src_sr=src_sr,
                    voice=vcn, fallback=fallback,
                    speed=round(speed / 50.0, 3), rate=float(rate))
        return wav, info

    # ---------------------------- ws 协议 ----------------------------

    def _ws(self, text, vcn, speed):
        """返回 (float32 PCM, 实际源码率)。

        24k 是按发音人授权的：不支持的音色会返回 code=0 + 空音频（不报错，
        坑过一次）。这里对空音频回落 16k 重试一次，并记住这个 vcn，
        免得每次朗读都白跑一趟。
        """
        hires = self.src_sr > 16000 and vcn not in self._no_hires
        if hires:
            pcm = self._ws_once(text, vcn, speed, config.XFYUN_AUF)
            if len(pcm):
                return pcm, self.src_sr
            log.info('音色 %s 不支持 %d Hz（返回空音频），回落 16000', vcn, self.src_sr)
            self._no_hires.add(vcn)
        return self._ws_once(text, vcn, speed, 'audio/L16;rate=16000'), 16000

    def _ws_once(self, text, vcn, speed, auf):
        import websocket

        appid, key, secret, _ = xfyun.tts_creds()
        url = xfyun.auth_url(config.XFYUN_TTS_WS_URL, key, secret)
        ws = websocket.create_connection(url, timeout=self.timeout)
        try:
            body = {
                'common': {'app_id': appid},
                'business': {'aue': 'raw',                 # raw = 裸 PCM，省一次解码
                             'auf': auf,
                             'vcn': vcn, 'tte': 'UTF8',
                             'speed': speed,
                             'volume': config.XFYUN_VOLUME,
                             'pitch': config.XFYUN_PITCH},
                # status=2：文本一次送完，服务端合成完就自己关连接
                'data': {'status': 2,
                         'text': base64.b64encode(text.encode('utf-8')).decode('ascii')},
            }
            ws.send(json.dumps(body))
            pcm = bytearray()
            while True:
                msg = json.loads(ws.recv())
                code = msg.get('code')
                if code != 0:
                    raise RuntimeError(f"讯飞返回错误 {code}: {msg.get('message')}")
                d = msg.get('data') or {}
                if d.get('audio'):
                    pcm.extend(base64.b64decode(d['audio']))
                if d.get('status') == 2:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return np.frombuffer(bytes(pcm), dtype='<i2').astype(np.float32) / 32768.0

    # ---------------------------- http 协议 ----------------------------

    def _http(self, text, vcn, rate):
        """通用 HTTP 口子：Bearer 鉴权 + JSON，响应认三种形态（见模块头注释）。"""
        import requests

        if not config.XFYUN_TTS_HTTP_URL:
            raise RuntimeError('TTS_XFYUN_HTTP_URL 没配，http 协议不可用')
        appid = config.XFYUN_APPID
        token = config.XFYUN_TTS_HTTP_KEY or config.XFYUN_API_PASSWORD or config.XFYUN_API_KEY
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        r = requests.post(config.XFYUN_TTS_HTTP_URL,
                          json={'text': text, 'voice': vcn, 'rate': float(rate),
                                'app_id': appid},
                          headers=headers, timeout=self.timeout)
        r.raise_for_status()
        ctype = (r.headers.get('Content-Type') or '').lower()
        if ctype.startswith('audio/') or r.content[:4] in (b'RIFF', b'ID3\x03'):
            return self._decode_audio(r.content, ctype)

        data = r.json()
        if isinstance(data, str):
            data = json.loads(data)
        b64 = (data.get('audio') or (data.get('data') or {}).get('audio')
               or data.get('audio_base64'))
        if b64:
            return self._decode_audio(base64.b64decode(b64), data.get('format') or '')
        url = data.get('url') or (data.get('data') or {}).get('url')
        if url:
            a = requests.get(url, timeout=self.timeout)
            a.raise_for_status()
            return self._decode_audio(a.content, a.headers.get('Content-Type') or '')
        raise RuntimeError(f'http 响应里没有音频字段：{str(data)[:200]}')

    def _decode_audio(self, raw, ctype):
        """WAV 走 soundfile；裸 PCM 直接按 int16 解；mp3 交给 soundfile 兜底。"""
        low = (ctype or '').lower()
        if raw[:4] == b'RIFF' or 'wav' in low:
            import io
            import soundfile as sf
            a, sr = sf.read(io.BytesIO(raw), dtype='float32', always_2d=False)
            if a.ndim > 1:
                a = a[:, 0]
            self.src_sr = int(sr)       # 代理给的采样率可能与约定不同，按实际的来
            return a
        if 'mpeg' in low or 'mp3' in low or raw[:3] == b'ID3':
            import io
            import soundfile as sf
            a, sr = sf.read(io.BytesIO(raw), dtype='float32', always_2d=False)
            if a.ndim > 1:
                a = a[:, 0]
            self.src_sr = int(sr)
            return a
        return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
