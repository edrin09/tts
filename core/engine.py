"""基座引擎（ONNX 版）：加载 ONNX 模型 → 预热 → 流式合成。

与旧 torch 版的唯一差别是**执行引擎**，对外接口完全一致：
* 声学模型由 onnxruntime 跑，进程里完全不 import torch
  实测同一段 97 字：冷启动 15.75s → 4.25s，峰值内存 1720MB → 975MB，RTF 0.368 → 0.293
* G2P 仍是 misaki 的 ZHG2P(version='1.1') + zh_patch，音素串与 torch 版逐字节一致
  （**version 必须显式传 '1.1'**：漏了就退回「四声被吞」的老 bug，而且不报错）
* 音色是 .bin（裸 float32 [510,256]），与 HF 的 .bin 同格式；按音素数取一行当 style
* vocab 直接读 config.json 的 'vocab'（171 项），不再需要 KModel

设计要点（沿用一期）
----------
* 模型只加载一次，常驻进程；音色表加载后缓存
* 合成按块进行（见 chunker），每块算完就吐给调用方，不等整段
* 每块只做一次 G2P：音素串同时用于算语速和送模型
* speed 用 chunker.speed_for，按音素数自动降速
* 块间插 TTS_CHUNK_GAP_MS 静音，块末不加
"""
import json
import time
from pathlib import Path

import numpy as np

from . import chunker, config, en_g2p, zh_patch

MAX_PHONEMES = 510  # 模型硬上限，超过必须截断（官方也是这么干的）
STYLE_ROWS = 510    # 音色包行数：按音素数取行，超出取最后一行


class Engine:
    def __init__(self, threads=None):
        self.threads = threads or config.THREADS
        self.sr = config.SAMPLE_RATE
        self.load_s = None
        self._sess = None
        self._g2p = None
        self._vocab = None
        self._packs = {}
        self._gap = np.zeros(int(self.sr * config.CHUNK_GAP_MS / 1000), dtype=np.float32)

    # ---------------------------- 生命周期 ----------------------------

    def load(self, verify_weight=True):
        import onnxruntime as ort

        if config.ZH_PATCH:
            zh_patch.install()

        model = Path(config.ONNX_MODEL)
        if not model.is_file():
            raise FileNotFoundError(f'ONNX 模型不存在: {model}')
        if verify_weight:
            size = model.stat().st_size
            if size != config.ONNX_BYTES:
                raise RuntimeError(
                    f'模型字节数不符 {size} != {config.ONNX_BYTES}，疑似损坏或被替换')

        t = time.perf_counter()

        # G2P：必须和现役同一套，version='1.1' 不可省
        # en_callable：中英混排里的英文段走 espeak-ng（不依赖 torch/spacy），
        #   不传就会退化成 ❓ 被静默吞掉。见 core/en_g2p.py
        from misaki import zh
        self._g2p = zh.ZHG2P(version=config.G2P_VERSION, en_callable=en_g2p.en_g2p)

        with open(config.MODEL_DIR / 'config.json', encoding='utf-8') as f:
            self._vocab = json.load(f)['vocab']

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads   # 默认会吃满物理核，实测 16 线程更慢 20–40%
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 注意：不要关 enable_cpu_mem_arena。实测只省 34MB，却让 RTF 从 0.293 掉到 0.414
        self._sess = ort.InferenceSession(str(model), providers=['CPUExecutionProvider'],
                                          sess_options=so)
        self.load_s = round(time.perf_counter() - t, 3)
        return self

    def pack(self, voice=None):
        """音色表 [510,256]，惰性加载并缓存；按音素数取行的动作在 _infer 里"""
        vid = config.voice_id(voice)
        if vid in self._packs:
            return vid, self._packs[vid]
        vp = config.voice_path(vid)
        if not vp.is_file():
            raise FileNotFoundError(f'音色不存在: {vp}')
        tab = np.fromfile(vp, dtype=np.float32).reshape(STYLE_ROWS, -1)
        self._packs[vid] = tab
        return vid, tab

    def warmup(self, text=None, voice=None):
        if not config.WARMUP and text is None:
            return 0.0
        t = time.perf_counter()
        self.synth(text or config.WARMUP_TEXT, voice)
        return round(time.perf_counter() - t, 3)

    # ---------------------------- 合成 ----------------------------

    def phonemes(self, text):
        return self._g2p(text)[0]

    def plan(self, text, first_limit=None):
        if first_limit is None and config.NOSPLIT_CHARS and len(text) <= config.NOSPLIT_CHARS:
            return [text.strip()]  # 短文本整句合成：语调最连贯，代价是首字变慢
        return chunker.split(text,
                             config.FIRST_CHUNK_MAX if first_limit is None else first_limit,
                             config.CHUNK_MAX, config.SECOND_CHUNK_MAX,
                             # 显式指定首块上限时（对比实验/自检）不走 ramp，否则
                             # 第 0 块按 first_limit、第 1 块却按 ramp[1]，两头对不上
                             ramp=None if first_limit is not None else config.CHUNK_RAMP)

    def _infer(self, ps, pack, speed):
        if len(ps) > MAX_PHONEMES:
            ps = ps[:MAX_PHONEMES]
        ids = [j for j in (self._vocab.get(c) for c in ps) if j is not None]
        input_ids = np.array([[0, *ids, 0]], dtype=np.int64)
        # 与 KPipeline.infer 一致：按（截断后）音素数取行，越界取最后一行
        style = np.ascontiguousarray(pack[min(len(ps) - 1, STYLE_ROWS - 1)]).reshape(1, -1)
        out = self._sess.run(None, {'input_ids': input_ids,
                                    'style': style,
                                    'speed': np.array([speed], dtype=np.float32)})[0]
        return np.asarray(out, dtype=np.float32).reshape(-1)

    def block(self, text, voice=None, rate=1.0):
        """把一段文字**一次性**合成一块，不再切分。

        服务端按块合成时必须用它：`synth()` 会走 `plan()`，而 plan 会在内部
        标点处继续切，把「一个块」变成多个块，块边界就跟任务的 plan 对不上了。
        """
        vid, pack = self.pack(voice)
        return self._block(text, pack, rate)

    def _block(self, text, pack, rate=1.0):
        t = time.perf_counter()
        ps = self.phonemes(text)
        # 最终语速 = 自动长句降速 × 用户倍速（两者相乘）
        speed = chunker.speed_for(len(ps), config.SPEED_BASE, config.SPEED_LONG_FROM,
                                  config.SPEED_LONG_FLOOR, config.SPEED_SLOPE) * rate
        a = self._infer(ps, pack, speed)
        info = dict(text=text, chars=len(text), phonemes=len(ps),
                    audio_s=round(len(a) / self.sr, 3),
                    gen_s=round(time.perf_counter() - t, 3),
                    speed=round(speed, 3), rate=rate)
        return a, info

    def stream(self, text, voice=None, first_limit=None, rate=1.0):
        """逐块产出 (audio, info)；首块出来的时刻即首字延迟"""
        vid, pack = self.pack(voice)
        for i, c in enumerate(self.plan(text, first_limit)):
            a, info = self._block(c, pack, rate)
            info = dict(info, index=i, first=(i == 0))
            if i:
                yield self._gap, dict(info, index=i, silence=True, gen_s=0.0)
            yield a, info

    def synth(self, text, voice=None, first_limit=None, rate=1.0):
        """整段合成，返回 (float32 mono 波形, 统计信息)"""
        t0 = time.perf_counter()
        parts, ttfb, chunks = [], None, 0
        for a, info in self.stream(text, voice, first_limit, rate):
            if info.get('silence'):
                parts.append(a)
                continue
            if ttfb is None:
                ttfb = time.perf_counter() - t0
            chunks += 1
            parts.append(a)
        wav = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
        total = len(wav) / self.sr
        spent = time.perf_counter() - t0
        return wav, dict(voice=config.voice_id(voice), chunks=chunks, chars=len(text),
                         total_s=round(total, 2), ttfb_s=round(ttfb or 0.0, 3),
                         spent_s=round(spent, 2),
                         rtf=round(spent / total, 3) if total else None)

    def save(self, text, path, voice=None, first_limit=None, rate=1.0):
        import soundfile as sf
        wav, info = self.synth(text, voice, first_limit, rate)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(p), wav, self.sr)
        info['wav'] = str(p)
        return info
