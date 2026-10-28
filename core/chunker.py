"""切块策略。

两条硬约束（都是实测踩出来的，别改）

1. 首块必须小
   首字延迟只看首块。每字约 0.18–0.25s，所以首块 6 字 ≈ 1.2–1.5s、
   10 字 ≈ 1.8–2.8s、19 字（一个完整小句）≈ 3.1–4.4s。
   切点退到 jieba 词边界，不切开词。

2. 首块之后不能见逗号就切
   每个块都有约 0.5s 的固定开销（模型按块独立前向 + 状态重建）。
   块数从 4 涨到 8，全长凭空多 4s 以上（实测 22s → 30s）。
   所以首块之后只在句末（。！？；）或累计超 CHUNK_MAX 才切。
"""
import jieba

SENT_END = '。！？；!?…\n'
CLAUSE_END = '，、：,:'


def word_cut(text, limit):
    """取前 limit 字，切点退到词边界（不切开词）；返回 (head, rest)"""
    if len(text) <= limit:
        return text, ''
    acc = 0
    for w in jieba.cut(text):
        if acc + len(w) > limit:
            break
        acc += len(w)
    acc = acc or limit  # 一个词就超限时退化为硬切，避免死循环
    return text[:acc], text[acc:]


def split(text, first_limit=None, chunk_max=60, second_limit=None, ramp=None):
    """首块=首小句（超限按词边界截断）；其后只在句末或超长才切；绝不句中硬切。

    second_limit：**第二块**的独立上限。这不是为了首字，是为了不断流——
    首块只有约 3s 音频，而第二块若直接放到 60 字（约 13s 音频 / 5s 合成），
    合成还没回来首块就播完了，起播处必然空 2s 左右。把第二块压到 ~14 字，
    它就能在首块播完之前落地，之后的 60 字块才接得上（实测合成倍率恒定
    ≈2.65×，块大小几乎不增加总耗时，所以这个「爬坡」几乎免费）。

    ramp：**逐块上限表**，比 second_limit 更细。给 [8, 10, 20, 40] 就是
    第 0 块 ≤8 字、第 1 块 ≤10 字、第 2 块 ≤20、第 3 块 ≤40，之后按 chunk_max。

    为什么需要它（实测，见 logs/e2e_first.txt）：
    只压首块是白费。起播门槛按「第二块要多久合成」推算，首块压到 6 字后
    dur0 掉到 2s，而第二块仍是 14~25 字（要 3.5~4.2s），门槛于是顶到 2.5s
    上限，首字从 1.5s 反而涨到 3.2~4.2s —— 等待从「合成」变成了「静音」。
    要让首字真的变快，**首块和第二块必须同时变小**，这就是 ramp：
    每块的音频时长都盖得住下一块的合成时间，累计缓冲不掉队。
    """
    limits = [int(x) for x in (ramp or []) if int(x) > 0]
    if limits:
        first_limit = limits[0]          # ramp 存在时第 0 块按 ramp[0]，不按 first_limit
    elif first_limit is None:
        first_limit = 10 ** 6
    chunks, buf, k = [], '', 0
    for ch in text:
        buf += ch
        if ch not in SENT_END and ch not in CLAUSE_END:
            continue
        if k == 0:
            # 首块：按词边界截断，切点落在词与词之间
            piece, buf = buf.strip(), ''
            head, rest = word_cut(piece, first_limit)
            chunks.append(head)
            buf = rest
            k += 1
            continue
        if k < len(limits):
            lim = limits[k]                       # ramp 优先
        elif k == 1 and second_limit:
            lim = int(second_limit)
        else:
            lim = chunk_max
        if ch in SENT_END or len(buf) >= lim:
            chunks.append(buf.strip())
            buf = ''
            k += 1
        elif len(buf) >= lim * 2:
            # 兜底：整段没有句读的长文（罕见），到两倍上限就硬切，别让 ramp 失效
            chunks.append(buf.strip())
            buf = ''
            k += 1
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def speed_for(len_phonemes, base=1.1, long_from=83, long_floor=183, slope=500.0):
    """官方 make_zh.py 的语速 callable：短句 1.0*base，音素越多越慢，触底后不再降。

    base 默认 1.1 —— 官方示例就是 speed*1.1，不是 1.0。
    """
    if len_phonemes <= long_from:
        coef = 1.0
    elif len_phonemes < long_floor:
        coef = 1 - (len_phonemes - long_from) / slope
    else:
        coef = 1 - (long_floor - long_from) / slope
    return coef * base
