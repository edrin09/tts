"""取选区：UIA（TextPattern.GetSelection）。

为什么只用 UIA
--------------
UIA 的 `TextPattern.GetSelection()` 不动剪贴板、不打扰用户，Word / Edge / Chrome /
VSCode / PDF 阅读器（大部分）都支持。划词浮窗是高频动作，不能每次选中都去改
用户剪贴板，所以不做 Ctrl+C 兜底——UIA 拿不到就不弹浮窗。
"""
import ctypes
import logging

log = logging.getLogger('tts.selection')

UIA_MAX_UP = 8


# ---------------------------- UIA ----------------------------

def _text_from(ctrl):
    """从控件拿选中文字；不支持的控件返回 ''。

    注意这里用的是 `GetPattern(PatternId.TextPattern)`，**不是** `ctl.TextControl()`：
    后者是「在子控件里搜一个类型为 TextControl 的元素」的搜索辅助方法，
    默认还会等 10 秒超时 —— 在按下快捷键的关键路径上用它，等于自废首字延迟。
    """
    import uiautomation as auto

    tp = None
    try:
        tp = ctrl.GetPattern(auto.PatternId.TextPattern)
    except Exception:
        tp = None
    if tp is None:
        return ''
    try:
        ranges = tp.GetSelection()
    except Exception:
        return ''
    parts = []
    for r in ranges:
        try:
            t = r.GetText(-1)
        except Exception:
            t = ''
        if t:
            parts.append(t)
    return ''.join(parts).strip()


def uia_selection():
    """从鼠标位置 / 焦点控件往上找能提供 TextPattern 的控件"""
    import uiautomation as auto

    starts = []
    for getter in (auto.ControlFromCursor, auto.GetFocusedControl):
        try:
            c = getter()
        except Exception:
            c = None
        if c:
            starts.append(c)

    visited = set()
    for c in starts:
        node = c
        for _ in range(UIA_MAX_UP):
            if node is None:
                break
            key = (getattr(node, 'NativeWindowHandle', 0), node.ControlTypeName,
                   getattr(node, 'ClassName', ''), getattr(node, 'Name', ''))
            if key in visited:
                break
            visited.add(key)
            text = _text_from(node)
            if text:
                return text, f'uia:{node.ControlTypeName}'
            try:
                node = node.GetParentControl()
            except Exception:
                break
    return '', ''

