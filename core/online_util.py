"""在线 TTS 引擎的公共零件：重采样、按句切块、块尾补标点。

为什么单独一个模块
------------------
讯飞（engine_xfyun）和 Edge（engine_edge）是两套完全不同的协议和音色体系，
但「怎么把一段文章切成块」这件事对它们是一样的，而且这套逻辑调了很久
（块边界落在句号上、尾块不许太短、块尾补句号让语调下沉收住），不该抄两份
—— 抄一份就意味着以后调听感要改两个地方，漏一个就会出现「讯飞好听了、
Edge 还是老毛病」这种鬼故事。

本地引擎（core/chunker.py）不走这里：本地要的是「首字最快」，会把句子按逗号
切碎做爬坡；在线引擎要的是「听着像人」，靠攒句换语调连贯。两套目标相反，
别合并。
"""
import numpy as np

_SENT = '。！？；!?…\n'


def resample(x, sr_from, sr_to):
    """线性重采样。只用于把 16k 的源上采样到 24k，别指望它做高质量变采样。"""
    if sr_from == sr_to or len(x) == 0:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0.0, len(x) - 1, n),
                     np.arange(len(x)), x).astype(np.float32)


def sentences(text):
    """按句末标点切成句子（标点留在句尾），不切逗号。"""
    out, buf = [], ''
    for ch in text:
        buf += ch
        if ch in _SENT:
            if buf.strip():
                out.append(buf.strip())
            buf = ''
    if buf.strip():
        out.append(buf.strip())
    return out


def split_long(s, limit, cut=None):
    """单句超长时的退路：按词边界硬切（万不得已，会损失语调）。

    cut 默认用 chunker.word_cut（jieba 词典，切在词边界上，不会把「银行」
    切成「银 / 行」）。传别的切词器可以换策略，但一般不用管。
    """
    if cut is None:
        from .chunker import word_cut as cut
    out = []
    while len(s) > limit:
        head, s = cut(s, limit)
        out.append(head.strip())
    if s.strip():
        out.append(s.strip())
    return [x for x in out if x]


def pack(sents, limit, cut=None):
    """把句子贪心打包成块：块边界永远落在句号上，逗号绝不切开。

    这是在线引擎听感的关键 —— 本地那套「首块=首小句」是为了首字速度，
    代价是句子被逗号切成碎片，每碎一次就多一次语调重置。
    """
    blocks, cur = [], ''
    for s in sents:
        if len(s) > limit:
            if cur:
                blocks.append(cur)
                cur = ''
            blocks.extend(split_long(s, limit, cut))
            continue
        if cur and len(cur) + len(s) > limit:
            blocks.append(cur)
            cur = ''
        cur += s
    if cur:
        blocks.append(cur)
    return blocks


def tail_punct(text, enabled=True):
    """块尾补一个句号：让语调在断口处自然下沉收住。

    切块是硬切的（块边界未必落在标点上），模型收到一个没有标点的结尾
    会用「还没说完」的平调收尾，接上下一块就成了生硬的断句。
    末尾是逗号/顿号/冒号的，直接换成句号（语气更完整）；已经带句末标点的原样返回。
    """
    s = (text or '').strip()
    if not s or not enabled:
        return s
    # 末尾是引号/括号时，看它里面那个字
    tail = s
    while len(tail) > 1 and tail[-1] in '”’」』）)》〉':
        tail = tail[:-1]
    last = tail[-1] if tail else ''
    if last in '，、：,:' :
        return tail[:-1] + '。' + s[len(tail):]
    if last in '。！？；!?….':
        return s
    return s + '。'


def plan_sentences(text, chunk_max, first_chunk_max, nosplit_chars, cut=None,
                   limit_floor=None):
    """在线引擎通用的切块：首块攒到 first 字，之后每块攒到 chunk_max 字。

    * nosplit_chars > 0 且文本不超过它 → 整段一块（听感最连贯，首字最慢）
    * 首块至少一整句（一句话被句中切开就是一个假句号 + 一段静音，听着就是「卡」）
    * 尾块太短就并进上一块（短尾块 = 一次额外请求 + 一次语调重置 + 尾巴挂静音）
    """
    text = (text or '').strip()
    if not text:
        return []
    if nosplit_chars and len(text) <= nosplit_chars:
        return [text]

    limit = chunk_max
    first_max = first_chunk_max
    sents = sentences(text)
    head, i, n = [], 0, 0
    while i < len(sents):
        s = sents[i]
        if head and (n + len(s) > first_max or n >= first_max):
            break
        head.append(s)
        n += len(s)
        i += 1
    first = ''.join(head) or text
    blocks = split_long(first, max(limit, len(first)), cut) if len(first) > limit else [first]
    blocks.extend(pack(sents[i:], limit, cut))
    if len(blocks) > 1 and len(blocks[-1]) <= first_max \
            and len(blocks[-2]) + len(blocks[-1]) <= limit:
        blocks[-2] += blocks[-1]
        blocks.pop()
    return [b for b in blocks if b]
