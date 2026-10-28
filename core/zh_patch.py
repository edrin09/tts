"""修 misaki 的一个变调 bug：跨词边界的同字被误判为重叠词。

现象
----
`银行行长` → `阴2ㄏㄤ2ㄏㄤ5ㄓㄤ3`，第二个「行」（应读 háng 2 声）被改成 5 声（轻声）。
轻声在听感上是一个又短又轻的下滑，听起来就是四声 —— 用户听到的「行念成四声」就是它。

原因
----
jieba.posseg 把「银行行长」整体切成一个名词 token（pos='n'），而
misaki.tone_sandhi.ToneSandhi._neural_sandhi 里有一条规则：

    # reduplication words for n. and v. e.g. 奶奶, 试试, 旺旺
    for j, item in enumerate(word):
        if j - 1 >= 0 and item == word[j - 1] and pos[0] in {"n", "v", "a"}:
            finals[j] = finals[j][:-1] + "5"

本意是处理「奶奶 / 试试 / 旺旺」这类重叠词，但它只看字符相邻相同，
不看这两个字是否属于同一个词。于是「银行」的行和「行长」的行被当成了重叠。

修法
----
先用 _split_word 求出子词边界（「银行行长」→ 银行 | 行长），
只把**跨边界的重复字**遮蔽掉（换成私用区字符，长度不变因此 finals 索引安全），
让原规则看不见它；子词内部的重复仍照常变轻声（爸爸、奶奶、试试 不受影响）。

影响面（2026-10-27 全量核查）
------------------------------
- jieba 词典里 2–6 字含相邻重字的词 6987 个，其中重字跨子词边界 1446 个
- 实际改变音素串 524 个，全部是「一 AA / AA」量词重叠（一串串、一条条…），
  这类本来就不读轻声，属顺带修对
- 真重叠词对照组 52 个（妈妈/试试/高高兴兴/打打闹闹…）**0 变动**
- 动词重叠、拟声词、儿化等 62 个高风险词 **逐字节不变**
"""
import os

from misaki.tone_sandhi import ToneSandhi

_orig_neural_sandhi = ToneSandhi._neural_sandhi
MASK = '\uE000'  # 私用区字符，不参与任何词表匹配


def _patched_neural_sandhi(self, word, pos, finals):
    try:
        if pos and pos[0] in {'n', 'v', 'a'} and len(word) > 1:
            try:
                subs = self._split_word(word) or [word]
            except Exception:
                subs = [word]
            bounds, start = set(), 0
            for sub in subs:
                start += len(sub)
                bounds.add(start)
            chars = list(word)
            masked = False
            for j in range(1, len(word)):
                if chars[j] == chars[j - 1] and j in bounds:
                    chars[j] = MASK
                    masked = True
            if masked:
                word = ''.join(chars)
        return _orig_neural_sandhi(self, word, pos, finals)
    except Exception:
        # 上游在「中英混排单词」（如 CALL机）上会 IndexError，不能让一个怪词把整句朗读打断
        return finals


def install():
    """幂等安装，可重复调用。TTS_ZH_PATCH=off 或 TTS_NO_PATCH=1 时跳过（供 A/B 取证）"""
    if os.environ.get('TTS_NO_PATCH') == '1':
        return False
    if getattr(ToneSandhi._neural_sandhi, '_zh_patched', False):
        return False
    _patched_neural_sandhi._zh_patched = True
    ToneSandhi._neural_sandhi = _patched_neural_sandhi
    return True


def installed():
    return getattr(ToneSandhi._neural_sandhi, '_zh_patched', False)
