"""语音识别（ASR）封装：按住 Alt 说话、松开出字。

模型（.env ASR_MODEL_TYPE / ASR_VARIANT）
----------------------------------------
- sensevoice + int8（默认）：SenseVoiceSmall 量化版，228MB 文件 / 实测常驻 299MB。
  非流式但中文精度高、自带标点与 ITN；partial 靠定期对已录音频重解码模拟，
  松手后整段终解。
- sensevoice + fp32 / streaming_zipformer：代码路径仍在，但模型目录已随封版
  清理移除，要用得先补下文件（见 core/config.py 的 _DEFAULT_ASR_DIR）。

生命周期：懒加载 + 关闭开关即卸载（LocalAsr.unload），不用不占内存。

为什么放应用进程而不是 9528 服务
--------------------------------
识别是「按住 Alt」这个交互的一部分，partial 文字要实时显示、松手要立刻打字，
中间隔一层 HTTP 只会增加复杂度。模型懒加载 + 仅在使用期间驻留，
不用 ASR 就不付内存。TTS 引擎切换只重启服务进程，互不影响。

在线通道
--------
讯飞听写在 core/asr_xfyun.py（流式 WS + HTTP 兜底），本文件只管本地模型。
"""
import base64
import gc
import io
import logging
import os
import re
import threading
import time
import wave

import numpy as np

from core import config

log = logging.getLogger('tts.asr')

# SenseVoice 富文本输出带 <|zh|><|NEUTRAL|><|Speech|> 这类标签，打字前剥掉
_TAG_RE = re.compile(r'<\|[^|>]*\|>')


def strip_tags(text):
    return _TAG_RE.sub('', text).strip()


def _resample(x, sr_from, sr_to):
    """线性重采样（语音识别对相位不敏感，线性插值够用）。"""
    if sr_from == sr_to or len(x) == 0:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0.0, len(x) - 1, n),
                     np.arange(len(x)), x).astype(np.float32)


def samples_to_wav_bytes(samples, sr=16000):
    """float32 单声道 → 16bit PCM WAV 字节（在线通道上传用）。"""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class LocalAsr:
    """本地流式识别器。懒加载，进程内只建一次。"""

    def __init__(self):
        self._rec = None
        self._lock = threading.Lock()
        self.load_s = None

    @property
    def loaded(self):
        return self._rec is not None

    def ensure_loaded(self):
        if self._rec is not None:
            return self._rec
        with self._lock:
            if self._rec is not None:
                return self._rec
            import sherpa_onnx
            d = str(config.ASR_MODEL_DIR)
            t0 = time.perf_counter()
            if config.ASR_MODEL_TYPE == 'sensevoice':
                # fp32 → model.onnx；int8 → model.int8.onnx（目录不同，见 .env）
                fn = 'model.int8.onnx' if config.ASR_VARIANT == 'int8' else 'model.onnx'
                mpath = f'{d}/{fn}'
                if not os.path.exists(mpath):
                    raise FileNotFoundError(
                        f'ASR 模型缺失: {mpath}（封版只保留 int8，补模型见 core/config.py）')
                rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=mpath,
                    tokens=f'{d}/tokens.txt',
                    num_threads=config.ASR_THREADS,
                    use_itn=config.ASR_USE_ITN,
                )
            else:
                mpath = f'{d}/encoder.int8.onnx'
                if not os.path.exists(mpath):
                    raise FileNotFoundError(
                        f'ASR 模型缺失: {mpath}（zipformer 已随封版移除，补模型见 core/config.py）')
                rec = sherpa_onnx.OnlineRecognizer.from_transducer(
                    encoder=mpath,
                    decoder=f'{d}/decoder.onnx',
                    joiner=f'{d}/joiner.int8.onnx',
                    tokens=f'{d}/tokens.txt',
                    num_threads=config.ASR_THREADS,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method='greedy_search',
                    # PTT 的结束时机由「松开 Alt」决定，端点检测反而会在句中停顿处
                    # 截断结果，关掉
                    enable_endpoint_detection=False,
                )
            self.load_s = round(time.perf_counter() - t0, 2)
            self._rec = rec
            log.info('本地 ASR 模型就绪 type=%s dir=%s load=%ss',
                     config.ASR_MODEL_TYPE, d, self.load_s)
            return rec

    def session(self):
        """开一个识别会话：accept() 喂 16k float32，partial() 取当前字，
        finish() 取终稿。两种模型同一接口。"""
        rec = self.ensure_loaded()
        if config.ASR_MODEL_TYPE == 'sensevoice':
            return _OfflineSession(rec)
        return _StreamingSession(rec)

    def unload(self):
        """把模型从内存放掉（关闭语音输入时调用）。

        实测 int8：加载 +299MB，del + gc.collect() 后回收 277MB；
        残留约 22MB 是 ONNX Runtime 的全局 Env 和线程池，进程生命周期内
        只付一次，放不掉也不值得为它折腾。

        进行中的会话若还引用着 recognizer，Python 引用计数会让对象活到
        会话结束才真正析构，所以调用方应先把会话取消掉（Dictate 已做）。
        """
        with self._lock:
            if self._rec is None:
                return False
            self._rec = None
        gc.collect()
        log.info('本地 ASR 模型已卸载')
        return True


class _StreamingSession:
    """流式 zipformer：逐帧解码，partial 是真实的增量结果。"""

    def __init__(self, rec):
        self.stream = rec.create_stream()

    def accept(self, samples):
        self.stream.accept_waveform(16000, samples)
        while rec.is_ready(self.stream):
            rec.decode_stream(self.stream)

    def partial(self):
        return rec.get_result(self.stream)

    def finish(self):
        self.stream.input_finished()
        while rec.is_ready(self.stream):
            rec.decode_stream(self.stream)
        return rec.get_result(self.stream)


class _OfflineSession:
    """SenseVoice（离线）：accept 只攒音频；partial 定期把已录内容整段重解码

    （模拟「边说边出字」，代价是解码时间随录音变长，所以用自适应节流：
    上次解码花了多久，就攒够几倍时长的音频再解下一次，保证解码不追着录音跑）。
    finish 整段终解一次。decode_stream 不能复用 stream，每次新建。
    """

    def __init__(self, rec):
        self._rec = rec
        self._buf = []
        self._n = 0                      # 已攒样本数
        self._decoded_n = 0              # 上次解码覆盖到的样本数
        self._partial = ''
        self._last_cost = 0.0            # 上次解码耗时（秒）

    def accept(self, samples):
        self._buf.append(samples)
        self._n += len(samples)

    def partial(self):
        # 自适应节流：新攒的音频 ≥ 上次解码耗时的 3 倍（且至少 1s）才重解
        new_s = (self._n - self._decoded_n) / 16000.0
        if new_s < max(1.0, self._last_cost * 3):
            return self._partial
        pcm = np.concatenate(self._buf) if len(self._buf) > 1 else self._buf[0]
        t0 = time.perf_counter()
        s = self._rec.create_stream()
        s.accept_waveform(16000, pcm)
        self._rec.decode_stream(s)
        self._partial = strip_tags(s.result.text)
        self._last_cost = time.perf_counter() - t0
        self._decoded_n = self._n
        return self._partial

    def finish(self):
        if self._n == 0:
            return ''
        pcm = np.concatenate(self._buf) if len(self._buf) > 1 else self._buf[0]
        s = self._rec.create_stream()
        s.accept_waveform(16000, pcm)
        self._rec.decode_stream(s)
        return strip_tags(s.result.text)


LOCAL = LocalAsr()
