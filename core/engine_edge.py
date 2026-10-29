"""在线引擎：Edge TTS（微软神经网络语音的公开朗读端点，免费、不要 key）。

为什么加它
----------
本地 Kokoro 音质一般、常驻 820MB；讯飞免费额度按次算（每日 500 次），长文
朗读很快就见底。Edge 是第三条路：**不限量、免费、不要凭据**，音质是目前这几个
里最像真人的（Azure Neural 同一批模型），代价是服务器在境外、要联网。

与其它引擎的关系
----------------
对外接口和 XfyunEngine 完全一致（load/warmup/plan/block/sr/threads），
TaskStore、播放器、缓存一行都不用改 —— 换引擎只是换 service.build() 里的一行。

实测数据（2026-10-29，本机直连，未走代理）
------------------------------------------
    首字节 1.2~1.5s，一整句（3.2s 音频）总耗时 1.5s
    输出 mp3 @24kHz，正好等于 TTS_SAMPLE_RATE，不需要重采样
    连续调用不会变快 —— 每次都要重新 TLS 握手（见下）

为什么每次都要重新建连
----------------------
本该复用 aiohttp 的 connector 省掉握手，但 aiohttp 3.14 的 ClientSession 默认
connector_owner=True：外部传进去的 connector 会被 session 关闭时一并关掉，
第二次就报 "Session is closed"。实测确认过，别在这上面浪费时间。
所以首字延迟里那 ~1.1s 是固定的握手成本 —— 想压下去只能自己实现 wss 长连接，
收益不值这个复杂度（块与播放是并行的，只有第一块要等）。

音色
----
`edge-tts --list-voices` 能看到全部（中文几十个）。这里的默认表只挑了朗读场景
好用的几个；.env 的 TTS_EDGE_VOICES 可以整表覆盖，格式和讯飞一样 "name:显示名"。
"""
import asyncio
import io
import logging
import threading
import time

import numpy as np

from . import config
from .online_util import plan_sentences, resample, tail_punct

log = logging.getLogger('tts.engine-edge')

# 托盘/浮窗可选的音色。.env 的 TTS_EDGE_VOICES 会整表覆盖它
DEFAULT_VOICES = [
    ('zh-CN-XiaoxiaoNeural', '晓晓 · 女（自然）'),
    ('zh-CN-XiaoyiNeural', '晓伊 · 女（活泼）'),
    ('zh-CN-YunjianNeural', '云健 · 男（沉稳）'),
    ('zh-CN-YunxiNeural', '云希 · 男（年轻）'),
    ('zh-CN-YunyangNeural', '云扬 · 男（播报）'),
    ('zh-CN-liaoning-XiaobeiNeural', '晓北 · 女（东北）'),
    ('zh-CN-shaanxi-XiaoniNeural', '晓妮 · 女（陕西）'),
    ('zh-HK-HiuMaanNeural', '曉曼 · 女（粤语）'),
    ('zh-TW-HsiaoChenNeural', '曉臻 · 女（台湾）'),
]


class EdgeEngine:
    sr = config.SAMPLE_RATE     # Edge 原生就是 24k，这里等于直通
    src_sr = 24000

    def __init__(self):
        self.threads = config.THREADS   # 没有线程池，但 /health 要报个数
        self.load_s = 0.0
        self.timeout = int(config.EDGE_TIMEOUT)
        self.connect_timeout = int(config.EDGE_CONNECT_TIMEOUT)
        self.retry = config.EDGE_RETRY
        self.pitch = self._pitch(config.EDGE_PITCH)
        self._lock = threading.Lock()   # 同一时刻只跑一次合成（对齐本地引擎的串行语义）

    # ---------------------------- 生命周期 ----------------------------

    def load(self):
        """校验依赖。edge-tts 是纯 Python 包，缺了要在启动日志里说人话。"""
        import edge_tts  # noqa: F401
        import soundfile  # noqa: F401   mp3 解码走它（libsndfile ≥1.1 才支持 mp3）
        return self

    def warmup(self, text=None, voice=None):
        """真合成一次：验证连通性，顺便把 DNS / TLS 会话票据预热掉。

        失败只警告不抛 —— 断网时服务照样能起来，用户切回来时自然会看到提示。
        """
        t = time.perf_counter()
        try:
            self.plan(text or config.WARMUP_TEXT)
        except Exception as e:
            log.warning('切块预热失败（不影响朗读）: %s', e)
        try:
            self.block(text or config.WARMUP_TEXT, voice)
            log.info('Edge 引擎预热完成 %.2fs（连通性 OK%s）',
                     time.perf_counter() - t,
                     f'，代理={config.EDGE_PROXY}' if config.EDGE_PROXY else '，直连')
        except Exception as e:
            log.warning('Edge 引擎预热失败（断网/端点变了？朗读时会重试）: %s', e)
        return round(time.perf_counter() - t, 3)

    # ---------------------------- 切块 ----------------------------

    def plan(self, text, first_limit=None):
        """按句打包切块（与讯飞同一套逻辑：句号才切，逗号不切）"""
        return plan_sentences(text,
                              config.EDGE_CHUNK_MAX,
                              config.EDGE_FIRST_CHUNK_MAX if first_limit is None
                              else first_limit,
                              config.EDGE_NOSPLIT_CHARS)

    # ---------------------------- 音色 / 语速 ----------------------------

    @staticmethod
    def _voice(voice):
        """female/male → 配置里的 Neural 名；已经是全名就直传。

        切引擎时偏好文件里可能留着别的引擎的音色（zf_001 / x4_yezi），
        传上去会被端点拒（Invalid voice），所以按前缀兜底成男女声。
        """
        v = (voice or config.VOICE_DEFAULT).strip()
        if v == 'female' or v.startswith(('zf_', 'x4_')):
            return config.EDGE_VOICE_FEMALE
        if v == 'male' or v.startswith(('zm_', 'ais', 'xiaoyan', 'xiaofeng')):
            return config.EDGE_VOICE_MALE
        return v

    @staticmethod
    def _rate(rate):
        """1.0× → TTS_EDGE_RATE_BASE（默认 +0%），倍速线性换算成 "+18%" 这种。

        Edge 的 rate 是相对语速的百分比，不是绝对档位；上下限官方给的是 ±100%，
        但超过 +60% 已经听不出字了，这里收到 [-40, +80] 之间。
        """
        pct = int(round((float(rate) - 1.0) * 100 + config.EDGE_RATE_BASE))
        pct = max(-40, min(80, pct))
        return f'{pct:+d}%'

    @staticmethod
    def _pitch(v):
        """音高必须是 "+0Hz" 这种绝对频偏写法；写成 "+0%" 会被端点拒。

        容错一下：不是 Hz 结尾的一律按 0 处理，免得因为一行 .env 写得随意，
        整个引擎每次合成都在重试 → 用户看到的是「朗读没反应」。
        """
        s = str(v or '').strip()
        if s.endswith('Hz') and len(s) > 2:
            return s
        if s and s not in ('+0%', '-0%'):
            log.warning('TTS_EDGE_PITCH=%r 不是 "+0Hz" 写法，按 +0Hz 处理', s)
        return '+0Hz'

    # ---------------------------- 合成 ----------------------------

    def block(self, text, voice=None, rate=1.0):
        """合成一个块（不再切分），返回 (float32 mono @sample_rate, info)"""
        t = time.perf_counter()
        text = text[:config.EDGE_MAX_CHARS]
        v = self._voice(voice)
        rate_s = self._rate(rate)
        send = tail_punct(text, config.EDGE_TAIL_PUNCT)

        last, raw = None, None
        for attempt in range(self.retry + 1):
            try:
                with self._lock:
                    raw = asyncio.run(self._synth_async(send, v, rate_s))
                break
            except Exception as e:
                last = e
                if attempt < self.retry:
                    # 端点偶尔抽风（TLS 断、403 过期 token），退一步重来通常就好了
                    log.warning('Edge 合成失败（第 %d 次），重试：%s', attempt + 1, e)
                    time.sleep(0.35)
        if raw is None:
            raise RuntimeError(f'Edge 合成失败：{type(last).__name__}: {last}') from last

        wav = self._decode(raw)
        if len(wav) == 0:
            raise RuntimeError('Edge 合成失败：服务返回了空音频')
        info = dict(text=text, sent=send, chars=len(text), phonemes=0,
                    audio_s=round(len(wav) / self.sr, 3),
                    gen_s=round(time.perf_counter() - t, 3), src_sr=self.src_sr,
                    voice=v, fallback=None,
                    speed=round((float(rate)), 3), rate=float(rate))
        return wav, info

    async def _synth_async(self, text, voice, rate_s):
        """跑一次 wss 流式合成，把音频块攒成 mp3 字节。

        edge-tts 内部已经处理了微软的 Sec-MS-GEC 校验（那个 token 每分钟变一次，
        自己实现很容易踩 403），所以这里只当普通客户端用。
        """
        import edge_tts

        comm = edge_tts.Communicate(
            text, voice,
            rate=rate_s,
            volume=config.EDGE_VOLUME,
            pitch=self.pitch,
            proxy=(config.EDGE_PROXY or None),
            connect_timeout=self.connect_timeout,
            receive_timeout=self.timeout,
        )
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk['type'] == 'audio' and chunk.get('data'):
                buf.extend(chunk['data'])
        return bytes(buf)

    def _decode(self, raw):
        """mp3 → float32。libsndfile ≥1.1 才认 mp3（本机 1.2.2，已在 load 里探过）。"""
        import soundfile as sf

        if not raw:
            return np.zeros(0, dtype=np.float32)
        a, sr = sf.read(io.BytesIO(raw), dtype='float32', always_2d=False)
        if a.ndim > 1:
            a = a[:, 0]
        self.src_sr = int(sr)
        return resample(a.astype(np.float32), self.src_sr, self.sr)
