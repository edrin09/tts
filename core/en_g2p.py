"""英文音素（仅用于中英混排里的英文段），不依赖 torch / spacy。

背景
----
Kokoro 的中文前端 `misaki.zh.ZHG2P` 遇到英文段时，会调用外部注入的 `en_callable(text) -> 音素串`：

    for en, zh in re.findall(r'([A-Za-z \'-]*[A-Za-z][A-Za-z \'-]*)|([^A-Za-z]+)', text):
        ...
        segments.append(self.unk if en_callable is None else en_callable(en))

这里用 `misaki.espeak.EspeakFallback`：Kokoro 官方给英文 OOV 兜底的同一套实现，
由 espeak-ng（C 库，约 10MB，随 `espeakng-loader` 提供 dll + data）出 IPA，再用 Kokoro 作者写的映射表
（`e^ɪ→A`、`a^ɪ→I`、`o^ʊ→O`、`a^ʊ→W`、`ɔ^ɪ→Y`、`ɚ→əɹ` …）转成模型训练时见过的音素集。
**零 torch、零 spacy**，输出：`API → ˌApˌiˈI`、`latency → lˈAtənsi`。
千万不能用回 `misaki.en.G2P`：那条路 `import spacy` → `import torch`，本项目
没有 torch，英文段会退化成 `❓`（vocab 里没有，合成时被过滤）＝ 英文被静默吞掉。

代价：比 `misaki.en`（词性标注 + 词典 + 重音）粗，缩写一律按字母拼读、多音词按拼写读。
对「API / GPU / latency」这类技术词足够；长英文段落会带 espeak 味，不如专用英文前端自然。
"""
from types import SimpleNamespace

_fb = None


def _fallback():
    """惰性构造：纯中文场景不需要 espeak-ng，别为它付初始化代价"""
    global _fb
    if _fb is None:
        from misaki.espeak import EspeakFallback
        _fb = EspeakFallback(british=False)   # 不传 version，与 KPipeline 默认一致（ɾ→T、ʔ→t）
    return _fb


def en_g2p(text):
    """英文片段 → Kokoro 音素串（签名符合 ZHG2P(en_callable=...) 的要求）"""
    text = (text or '').strip()
    if not text:
        return ''
    ps, _ = _fallback()(SimpleNamespace(text=text))
    return ps or ''
