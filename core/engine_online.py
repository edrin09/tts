"""在线引擎：微软 Edge TTS（WebSocket 协议，免 key），MP3 24kHz → float32 PCM。

与本地 ONNX Engine 对外接口完全一致（plan / block / sr / threads / warmup），
TaskStore 无感知 —— 换引擎只是换掉 service.build() 里的那一行。

为什么走 edge-tts 包而不是手抄协议
----------------------------------
协议（Sec-MS-GEC token 之类）微软会不定期改，上游包维护者跟得比我们快，
端点偶尔 403 时 `pip install -U edge-tts` 就能修。参考 dsh-plugin-tts 的做法。

内存账：不 import onnxruntime、不加载 G2P，进程常驻 ~100MB（对比本地 ~820MB）。
代价是每个块一次网络往返（实测 4.9s 音频 1.6s 取回，约 3 倍实时）+ 断网不可用。
"""
import asyncio
import io
import logging
import time

import numpy as np
import soundfile as sf

from . import chunker, config

log = logging.getLogger('tts.engine-online')

# female / male → Edge 神经音色（听感分别接近本地 zf_001 / zm_029 的男女声）
VOICE_MAP = {
    'female': 'zh-CN-XiaoxiaoNeural',
    'male': 'zh-CN-YunxiNeural',
}

# 托盘可选的在线音色（id 直接透传给 Edge，label 给菜单显示）。
# 本地引擎只有 zf_001/zm_029 两个音色，这些只在在线模式可选（托盘里会置灰）
ONLINE_VOICES = [
    ('zh-CN-XiaoxiaoNeural', '晓晓 · 女 温柔'),
    ('zh-CN-XiaoyiNeural',   '晓伊 · 女 活泼'),
    ('zh-CN-XiaomoNeural',   '晓墨 · 女 沉稳'),
    ('zh-CN-YunxiNeural',    '云希 · 男 年轻'),
    ('zh-CN-YunjianNeural',  '云健 · 男 浑厚'),
    ('zh-CN-YunyangNeural',  '云扬 · 男 播音'),
    ('zh-CN-YunzeNeural',    '云野 · 男 温和'),
]


class EdgeEngine:
    sr = 24000  # Edge TTS 输出就是 24kHz，与本地一致

    def __init__(self):
        self.threads = config.THREADS   # 没有线程池，但 /health 要报个数
        self.load_s = 0.0

    # ---------------------------- 生命周期 ----------------------------

    def load(self):
        # 无模型可加载；.venv 里没装 edge-tts 也到这一步才发现不了，
        # 所以在这里显式 import 一次，让启动日志里出现人话报错
        import edge_tts  # noqa: F401
        return self

    def warmup(self, text=None, voice=None):
        """轻预热：主要目的是启动时验证一次连通性。失败不拖垮服务。"""
        t = time.perf_counter()
        try:
            self.block(text or config.WARMUP_TEXT, voice)
            log.info('在线引擎预热完成 %.2fs（连通性 OK）', time.perf_counter() - t)
        except Exception as e:
            log.warning('在线引擎预热失败（断网？朗读时会重试）: %s', e)
        return round(time.perf_counter() - t, 3)

    # ---------------------------- 切块 / 合成 ----------------------------

    def plan(self, text, first_limit=None):
        """切块规则与本地引擎完全一致 —— 任务的块边界、上一句/下一句都不变"""
        if first_limit is None and config.NOSPLIT_CHARS and len(text) <= config.NOSPLIT_CHARS:
            return [text.strip()]
        return chunker.split(text,
                             config.FIRST_CHUNK_MAX if first_limit is None else first_limit,
                             config.CHUNK_MAX, config.SECOND_CHUNK_MAX,
                             ramp=None if first_limit is not None else config.CHUNK_RAMP)

    def _voice(self, voice):
        v = config.voice_id(voice)
        if v in VOICE_MAP:
            return VOICE_MAP[v]
        return v if v and '-' in v else VOICE_MAP['female']  # 直接给了 Edge 音色名就放行

    @staticmethod
    def _rate_pct(rate):
        pct = round((float(rate) - 1.0) * 100)
        return f'{pct:+d}%'

    def block(self, text, voice=None, rate=1.0):
        """合成一个块（不切分），返回 (float32 mono, info) —— 与本地 Engine.block 同构"""
        import edge_tts

        t = time.perf_counter()
        mp3 = bytearray()
        try:
            c = edge_tts.Communicate(text, self._voice(voice), rate=self._rate_pct(rate))

            async def _fetch():
                async for chunk in c.stream():
                    if chunk['type'] == 'audio':
                        mp3.extend(chunk['data'])

            asyncio.run(_fetch())
        except Exception as e:
            raise RuntimeError(f'在线合成失败（检查网络）: {type(e).__name__}: {e}') from e
        if not mp3:
            raise RuntimeError('在线合成失败：服务返回了空音频')

        wav, sr = sf.read(io.BytesIO(bytes(mp3)), dtype='float32', always_2d=False)
        if wav.ndim > 1:
            wav = wav[:, 0]
        if sr != self.sr:  # 上游改采样率时兜底重采样，时间轴元数据只认引擎的 sr
            n = int(round(len(wav) * self.sr / sr))
            wav = np.interp(np.linspace(0, len(wav) - 1, n),
                            np.arange(len(wav)), wav).astype(np.float32)
        info = dict(text=text, chars=len(text), phonemes=0,
                    audio_s=round(len(wav) / self.sr, 3),
                    gen_s=round(time.perf_counter() - t, 3),
                    speed=round(float(rate), 3), rate=float(rate))
        return wav, info
