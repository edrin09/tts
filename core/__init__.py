"""本地 TTS 朗读 — 基座。

分层：
    config.py    读 .env，不散落硬编码
    zh_patch.py  修 misaki 跨词同字被判重叠词的变调 bug（必须最早安装）
    chunker.py   切块策略（首块压延迟 + 其后按句打包）
    engine.py    加载模型、预热、流式合成
"""
