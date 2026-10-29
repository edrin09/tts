"""读 .env 里的 TTS_ 配置。

优先级：进程环境变量 > 项目根 .env > 代码兜底默认值。

.env 用 CRLF + UTF-8 无 BOM 保存（cmd 的 for /f 只认 CRLF），
这里两种换行都能读，值不加引号、行尾不留空格。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / '.env'


def _load_env(path=ENV_FILE):
    data = {}
    if not Path(path).is_file():
        return data
    raw = Path(path).read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):  # 容忍 BOM
        raw = raw[3:]
    for line in raw.decode('utf-8', errors='replace').splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, v = s.split('=', 1)
        data[k.strip()] = v.strip()
    return data


_ENV = _load_env()


def get(key, default=None):
    """环境变量优先，其次 .env，最后兜底值"""
    if key in os.environ:
        return os.environ[key]
    return _ENV.get(key, default)


def get_int(key, default):
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        return default


def get_float(key, default):
    try:
        return float(get(key, default))
    except (TypeError, ValueError):
        return default


def get_bool(key, default=False):
    v = get(key)
    if v is None:
        return default
    return str(v).strip().lower() in {'1', 'on', 'true', 'yes', 'y'}


def get_path(key, default):
    v = get(key)
    return Path(v) if v else Path(default)


def set_env(key, value):
    """写回 .env（保留注释与其它行），并同步内存缓存让本次进程立刻可见。

    .env 保持 CRLF + UTF-8 无 BOM（见文件头注释）。托盘切引擎就用它：
    改完 .env 重启服务进程，新进程 import config 时自然读到新值。
    """
    lines = []
    if ENV_FILE.is_file():
        raw = ENV_FILE.read_bytes()
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        lines = raw.decode('utf-8', errors='replace').splitlines()
    found = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and not s.startswith('#') and '=' in s and s.split('=', 1)[0].strip() == key:
            lines[i] = f'{key}={value}'
            found = True
            break
    if not found:
        lines.append(f'{key}={value}')
    ENV_FILE.write_bytes(('\r\n'.join(lines) + '\r\n').encode('utf-8'))
    _ENV[key] = str(value)


# ---------------------------- 具体配置 ----------------------------

MODEL_DIR = get_path('TTS_MODEL_DIR', ROOT / 'models' / 'Kokoro-ONNX-v1.1-zh')
VOICES_DIR = get_path('TTS_VOICES_DIR', MODEL_DIR / 'voices')
CACHE_DIR = get_path('TTS_CACHE_DIR', ROOT / 'cache')

REPO_ID = get('TTS_REPO_ID', 'onnx-community/Kokoro-82M-v1.1-zh-ONNX')
LANG_CODE = get('TTS_LANG_CODE', 'z')
SAMPLE_RATE = get_int('TTS_SAMPLE_RATE', 24000)

# ONNX 模型文件（无 .pth 权重，模型就是这个 onnx 文件）
ONNX_MODEL = get_path('TTS_ONNX_MODEL', MODEL_DIR / 'onnx' / 'model.onnx')
ONNX_BYTES = get_int('TTS_ONNX_BYTES', 339369442)
ONNX_SHA256 = get('TTS_ONNX_SHA256', '')
# G2P 版本：必须是 1.1（中文优化版）。漏了不报错，只会让四声被吞
G2P_VERSION = get('TTS_G2P_VERSION', '1.1')

VOICE_FEMALE = get('TTS_VOICE_FEMALE', 'zf_001')
VOICE_MALE = get('TTS_VOICE_MALE', 'zm_029')
VOICE_DEFAULT = get('TTS_VOICE_DEFAULT', 'female')

SPEED_BASE = get_float('TTS_SPEED_BASE', 1.1)
SPEED_LONG_FROM = get_int('TTS_SPEED_LONG_FROM', 83)
SPEED_LONG_FLOOR = get_int('TTS_SPEED_LONG_FLOOR', 183)
SPEED_SLOPE = get_float('TTS_SPEED_SLOPE', 500)

FIRST_CHUNK_MAX = get_int('TTS_FIRST_CHUNK_MAX', 10)
# 第二块上限（字）：为「起播不断流」做爬坡，见 chunker.split 的说明
SECOND_CHUNK_MAX = get_int('TTS_SECOND_CHUNK_MAX', 14)
# 逐块上限表（字），覆盖上面两个值：「8,10,20,40」= 前四块分别 ≤8/10/20/40 字。
# 只压首块是白费：第二块不变大小时，起播门槛照样把它等回来（实测见 chunker.split）。
# 留空则退回 FIRST_CHUNK_MAX + SECOND_CHUNK_MAX 的老规则。
CHUNK_RAMP = [int(x) for x in str(get('TTS_CHUNK_RAMP', '8,10,20,40')).replace('，', ',')
              .split(',') if x.strip()]
# 短文本整句合成的阈值（字）：≤ 它的文本不切块
# 0 = 关闭（永远按 first_chunk 切，首字最快）；调大可换整句听感
NOSPLIT_CHARS = get_int('TTS_NOSPLIT_CHARS', 0)
CHUNK_MAX = get_int('TTS_CHUNK_MAX', 60)
CHUNK_GAP_MS = get_int('TTS_CHUNK_GAP_MS', 50)

HOST = get('TTS_HOST', '127.0.0.1')
PORT = get_int('TTS_PORT', 9528)
THREADS = get_int('TTS_THREADS', 8)
WARMUP = get_bool('TTS_WARMUP', True)
WARMUP_TEXT = get('TTS_WARMUP_TEXT', '预热。')

ZH_PATCH = get_bool('TTS_ZH_PATCH', True)

# 合成引擎：
#   local = 本地 ONNX（~820MB，离线，音质一般）
#   xfyun = 讯飞在线（~100MB，需联网，免费额度按次算）
#   edge  = Edge TTS（~100MB，需联网，免费不限量，音质最好，境外端点偶有抖动）
# 托盘「语音引擎」或 run.py --engine 切换，改的是 .env 里这一项，重启服务进程后生效
ENGINE = get('TTS_ENGINE', 'local').strip().lower()
# 兼容旧值：早期用 online 表示 Edge
if ENGINE in ('online', 'edgetts'):
    ENGINE = 'edge'
if ENGINE not in ('local', 'xfyun', 'edge'):
    ENGINE = 'local'


def _protocols(key, default='ws'):
    """把 "ws,http" 解析成 ['ws','http']（顺序即回落顺序），非法值退回默认。"""
    out = [x.strip().lower() for x in str(get(key, default) or default).split(',') if x.strip()]
    out = [x for x in out if x in ('ws', 'http')]
    return out or ['ws']


# ---------------------------- 讯飞（在线 TTS + 听写） ----------------------------

# 应用凭据：控制台 → 我的应用 → 语音合成/语音听写 → 服务接口认证信息
# 同一套 APPID/APIKey/APISecret 就够两个服务用；听写想用别的应用再填 ASR_XFYUN_*
XFYUN_APPID = (get('TTS_XFYUN_APPID', '') or '').strip()
XFYUN_API_KEY = (get('TTS_XFYUN_API_KEY', '') or '').strip()
XFYUN_API_SECRET = (get('TTS_XFYUN_API_SECRET', '') or '').strip()
# 新控制台另给的 API 密码（ak- 开头），http 模式没单独配 key 时拿它当 Bearer
XFYUN_API_PASSWORD = (get('TTS_XFYUN_API_PASSWORD', '') or '').strip()

# 协议：ws（官方 WebAPI，默认）| http（自建代理/兼容服务）| ws,http（先 ws 失败回落）
XFYUN_TTS_PROTOCOLS = _protocols('TTS_XFYUN_PROTOCOL', 'ws')
XFYUN_TTS_WS_URL = get('TTS_XFYUN_WS_URL', 'wss://tts-api.xfyun.cn/v2/tts')
XFYUN_TTS_HTTP_URL = (get('TTS_XFYUN_HTTP_URL', '') or '').strip()
XFYUN_TTS_HTTP_KEY = (get('TTS_XFYUN_HTTP_KEY', '') or '').strip()

# 输出格式：raw PCM。采样率写在 auf 里。
# ⚠ 24k 是**按发音人分开授权**的，实测（2026-10-29）：
#     支持 24k：x4_yezi / aisjiuxu / aisxping / aisjinger / aisbabyxu
#     不支持  ：xiaoyan / xiaofeng（返回 code=0 但空音频）
#   引擎会对「返回空音频」的音色自动回落 16k 并记住，不用手工改这一项。
XFYUN_AUF = get('TTS_XFYUN_AUF', 'audio/L16;rate=24000')
try:
    XFYUN_SRC_SR = int(str(XFYUN_AUF).split('rate=')[-1])
except ValueError:
    XFYUN_SRC_SR = 24000

# 语速基准：1.0× 对应的讯飞 speed（0~100，50＝自然语速），倍速按它线性缩放
# 46 比 50 略慢一点：老发音人在 50 上赶得像播报，慢半档反而更像说话
XFYUN_SPEED_BASE = get_int('TTS_XFYUN_SPEED_BASE', 44)
# 音高 / 音量（0~100，50＝默认）。音高降→更沉更稳，升→更亮更年轻
XFYUN_PITCH = get_int('TTS_XFYUN_PITCH', 48)
XFYUN_VOLUME = get_int('TTS_XFYUN_VOLUME', 55)
# 块尾补句号：切块把长句拦腰截断时，末尾补一个「。」让语调自然下沉收住，
# 不然模型会把断口当成没说完，听起来就是「半句话硬掐断」
XFYUN_TAIL_PUNCT = get_bool('TTS_XFYUN_TAIL_PUNCT', True)
# 单次调用文本上限（字）：官方 <8000 字节≈2000 汉字，留点余量
XFYUN_MAX_CHARS = get_int('TTS_XFYUN_MAX_CHARS', 1800)
# 网络超时（秒）：一个块最多等这么久，超时按失败处理（ws 有回落就回落 http）
XFYUN_TIMEOUT = get_float('TTS_XFYUN_TIMEOUT', 20.0)

# 男女声映射：逻辑音色 female/male → 讯飞 vcn
# 默认 female 用超拟人「叶子」：5 个免费基础发音人都是 2016 年的老模型，
# 听感生硬；本账号实测 x4_yezi 可用，语调/停顿明显自然一档
XFYUN_VOICE_FEMALE = (get('TTS_XFYUN_VOICE_FEMALE', 'x4_yezi') or 'x4_yezi').strip()
XFYUN_VOICE_MALE = (get('TTS_XFYUN_VOICE_MALE', 'aisjiuxu') or 'aisjiuxu').strip()
# 授权失效兜底：特色/超拟人发音人是限时试用（15 天 / 3 个月），到期接口返回 11200
# licc failed。这时不报错，直接换成这个永久免费的基础发音人继续念，避免朗读中断。
XFYUN_FALLBACK_VOICE = (get('TTS_XFYUN_FALLBACK_VOICE', 'xiaoyan') or 'xiaoyan').strip()


def _voices(key, default):
    """解析 "vcn:标签,vcn:标签" → [(vcn, 标签)]，托盘/浮窗下拉用。"""
    raw = get(key, '') or ''
    out = []
    for item in str(raw).replace('，', ',').split(','):
        item = item.strip()
        if not item:
            continue
        vcn, _, label = item.partition(':')
        vcn = vcn.strip()
        if vcn:
            out.append((vcn, (label or vcn).strip()))
    return out or list(default)


XFYUN_VOICES = _voices('TTS_XFYUN_VOICES', [
    ('x4_yezi', '叶子 · 超拟人'),
    ('aisjiuxu', '许久 · 男'),
    ('aisxping', '小萍 · 女'),
    ('aisjinger', '小婧 · 女'),
    ('aisbabyxu', '许小宝 · 童'),
])

# ---- 讯飞自己的切块参数 ----
# 为什么和本地分开：本地模型 RTF≈2.65（合成比播放慢），必须小块爬坡才不断流；
# 讯飞是云合成，块边界＝语调重置，所以改成「按句打包」——句号才切，
# 逗号绝不切。块越大越连贯，每日 500 次免费额度也越省。
XFYUN_FIRST_CHUNK_MAX = get_int('TTS_XFYUN_FIRST_CHUNK_MAX', 30)
XFYUN_CHUNK_MAX = get_int('TTS_XFYUN_CHUNK_MAX', 80)
# ≤ 这么多字不切块，整段一次合成（0＝关，永远按 ramp 切）
XFYUN_NOSPLIT_CHARS = get_int('TTS_XFYUN_NOSPLIT_CHARS', 0)
# 块间留白（毫秒）：播放器按时间轴真的补静音。50ms 听着像「连读硬切」，
# 300ms 才有换气的意思 —— 这是「没停顿」最直接的旋钮
XFYUN_CHUNK_GAP_MS = get_int('TTS_XFYUN_CHUNK_GAP_MS', 300)

# ---------------------------- Edge TTS（第三个引擎：免费不限量） ----------------------------
#
# 走微软 Edge 浏览器的「大声朗读」端点（speech.platform.bing.com），不用注册、
# 不用 key、不计次。音质是这三个里最好的（Azure Neural 同一批模型）。
# 代价：服务器在境外，断网/被墙就没了；首字比讯飞慢（每次合成都要重新 TLS 握手）。
#
# 代理：留空＝直连（实测本机直连 1.2s 出音，够用）。
# 所在网络直连不通时填本地代理，例如 http://127.0.0.1:7890 —— 只影响 Edge 引擎，
# 不会动系统代理设置，也不会影响讯飞/本地引擎。
EDGE_PROXY = (get('TTS_EDGE_PROXY', '') or '').strip()

# 单次合成最长文本（字）。Edge 对超长文本会中途断流，一段 2000 字以内是安全的；
# 正常流程里块只有 60~80 字，这里只是防「直接调 block() 塞了一整篇文章」。
EDGE_MAX_CHARS = get_int('TTS_EDGE_MAX_CHARS', 2000)

# 网络超时（秒）。connect 是握手，receive 是收音频；境外线路抖一下很正常，
# 给宽一点，反正失败还有 EDGE_RETRY 次重试。
# ⚠ 必须是整数：edge-tts 用 isinstance(x, int) 校验，写 10.0 会直接 TypeError
EDGE_CONNECT_TIMEOUT = get_int('TTS_EDGE_CONNECT_TIMEOUT', 10)
EDGE_TIMEOUT = get_int('TTS_EDGE_TIMEOUT', 30)
# 失败重试次数（0＝不重试）。端点偶发 403/断流，重试一次基本都能过
EDGE_RETRY = get_int('TTS_EDGE_RETRY', 1)

# 1.0× 对应的相对语速（%）。Edge 默认语速偏快，朗读长文可以设 -5
EDGE_RATE_BASE = get_int('TTS_EDGE_RATE_BASE', 0)
# 音量 / 音高。音量是相对百分比（"+0%"）；**音高是绝对频偏，必须带 Hz**
# （"+0Hz"），写成 "+0%" 会被端点拒（Invalid pitch）。引擎侧会兜底纠正，
# 但别依赖兜底。音高别乱调，±10Hz 就开始不像真人
EDGE_VOLUME = get('TTS_EDGE_VOLUME', '+0%')
EDGE_PITCH = get('TTS_EDGE_PITCH', '+0Hz')

# 逻辑音色 female / male → Neural 音色名
# 晓晓是最自然的中文女声；云健（Yunjian）是男声里最适合长篇朗读的（沉稳、不飘）
EDGE_VOICE_FEMALE = (get('TTS_EDGE_VOICE_FEMALE', 'zh-CN-XiaoxiaoNeural')
                     or 'zh-CN-XiaoxiaoNeural').strip()
EDGE_VOICE_MALE = (get('TTS_EDGE_VOICE_MALE', 'zh-CN-YunjianNeural')
                   or 'zh-CN-YunjianNeural').strip()
EDGE_VOICES = _voices('TTS_EDGE_VOICES', [
    ('zh-CN-XiaoxiaoNeural', '晓晓 · 女（自然）'),
    ('zh-CN-YunxiNeural', '云希 · 男（年轻）'),
    ('zh-CN-YunjianNeural', '云健 · 男（沉稳）'),
    ('zh-CN-YunyangNeural', '云扬 · 男（播报）'),
    ('zh-CN-XiaoyiNeural', '晓伊 · 女（活泼）'),
])

# ---- Edge 自己的切块参数 ----
# 和讯飞同一套「按句打包」，但块更小一点：Edge 不限量，不用为省次数攒大块，
# 小块的好处是起播快、跳转（上一句/下一句）粒度细，且断流时损失小。
EDGE_FIRST_CHUNK_MAX = get_int('TTS_EDGE_FIRST_CHUNK_MAX', 24)
EDGE_CHUNK_MAX = get_int('TTS_EDGE_CHUNK_MAX', 60)
EDGE_NOSPLIT_CHARS = get_int('TTS_EDGE_NOSPLIT_CHARS', 0)
# 块间留白：每块独立合成，留白做出「换气」。嫌拖就 200，嫌赶就 400
EDGE_CHUNK_GAP_MS = get_int('TTS_EDGE_CHUNK_GAP_MS', 250)
# 块尾补句号（1 开 / 0 关）：硬切断时让语调自然收住，别留「没说完」的平调
EDGE_TAIL_PUNCT = get_bool('TTS_EDGE_TAIL_PUNCT', True)

# 实际生效的块间留白：本地要小（连续流式，且每块合成慢，留白会显得断），
# 在线要大（每块独立合成，靠留白做出「换气」）。切换引擎会重启服务，所以这里
# 按进程启动时的 ENGINE 静态决定即可，不用做成运行时可变。
if ENGINE == 'local':
    GAP_MS = CHUNK_GAP_MS
elif ENGINE == 'edge':
    GAP_MS = EDGE_CHUNK_GAP_MS
else:
    GAP_MS = XFYUN_CHUNK_GAP_MS

# ---- 听写（语音输入）----
# 留空 = 沿用上面 TTS_XFYUN_* 那一套（同一个应用即可）
ASR_XFYUN_APPID = (get('ASR_XFYUN_APPID', '') or '').strip()
ASR_XFYUN_API_KEY = (get('ASR_XFYUN_API_KEY', '') or '').strip()
ASR_XFYUN_API_SECRET = (get('ASR_XFYUN_API_SECRET', '') or '').strip()
XFYUN_ASR_PROTOCOLS = _protocols('ASR_XFYUN_PROTOCOL', 'ws')
XFYUN_ASR_WS_URL = get('ASR_XFYUN_WS_URL', 'wss://iat-api.xfyun.cn/v2/iat')
XFYUN_ASR_HTTP_URL = (get('ASR_XFYUN_HTTP_URL', '') or '').strip()
XFYUN_ASR_HTTP_KEY = (get('ASR_XFYUN_HTTP_KEY', '') or '').strip()
# 口音：mandarin（普通话）| cantonese | sichuanese | henanese ...
XFYUN_ASR_ACCENT = (get('ASR_XFYUN_ACCENT', 'mandarin') or 'mandarin').strip()
# 建 WS 握手的超时（秒）：按住 Alt 后要等它连上才出字，太长就等于「没反应」，
# 太短则网络抖一下就失败。8s 是折中；超时会弹小窗提示，不再静默卡住
XFYUN_ASR_CONNECT_TIMEOUT = get_float('ASR_XFYUN_CONNECT_TIMEOUT', 8.0)

# ---------------------------- 服务 / 播放 / 倍速 ----------------------------

# 日志体系已拆除（无 LOG_DIR、不落盘）；见 run.py / service/server.py 的 disable 说明

# 用户看到的倍速档位；内部 1.0× 对应的真实 speed 是 SPEED_BASE(1.1)
RATE_DEFAULT = get_float('TTS_RATE_DEFAULT', 1.0)
# 自定义倍速的允许范围：浮窗「自定义」输入框按它校验，偏好存盘也按它兜底
RATE_MIN = get_float('TTS_RATE_MIN', 0.5)
RATE_MAX = get_float('TTS_RATE_MAX', 3.0)

# 播放器向前预取的缓冲秒数（≥3s 才能盖住单块合成耗时）
PREFETCH_S = get_float('TTS_PREFETCH_S', 3.0)
# 预取高水位：低于它就继续拉下一块（只按 3s 预取会让 60 字块来不及合成）
BUFFER_HIGH_S = get_float('TTS_BUFFER_HIGH_S', 20.0)
# 起播门槛上限（秒）：预计断流时先攒缓冲再起播，但最多多等这么久
START_GATE_MAX_S = get_float('TTS_START_GATE_MAX_S', 2.5)

# 缓存/任务清理
CACHE_MAX_MB = get_int('TTS_CACHE_MAX_MB', 200)
CACHE_SALT = get('TTS_CACHE_SALT', 'v2-xfyun-prosody')
# 在线引擎把「听感参数」也拼进缓存键：改语速/音高/音量/补标点/切块，
# 旧缓存自动失效。不这么做的话，改完参数再读同一段，命中的还是旧音频 ——
# 表现就是「改了跟没改一样」，这一坑在调听感时特别容易误判。
if ENGINE == 'xfyun':
    CACHE_SALT = (f'{CACHE_SALT}-s{XFYUN_SPEED_BASE}p{XFYUN_PITCH}'
                  f'v{XFYUN_VOLUME}t{int(XFYUN_TAIL_PUNCT)}'
                  f'f{XFYUN_FIRST_CHUNK_MAX}c{XFYUN_CHUNK_MAX}g{GAP_MS}')
elif ENGINE == 'edge':
    # Edge 的听感旋钮是「相对语速 + 音高 + 音量 + 块大小」，同样拼进 key
    CACHE_SALT = (f'{CACHE_SALT}-edge-r{EDGE_RATE_BASE}p{EDGE_PITCH}'
                  f'v{EDGE_VOLUME}t{int(EDGE_TAIL_PUNCT)}'
                  f'f{EDGE_FIRST_CHUNK_MAX}c{EDGE_CHUNK_MAX}g{GAP_MS}')
TASK_KEEP = get_int('TTS_TASK_KEEP', 20)

# 选中浮窗（松手在鼠标旁弹一个「朗读」按钮，点一下就读）
BUBBLE = get_bool('TTS_BUBBLE', True)
# 松手后等这么久再取字：刚拖完选区的瞬间，应用的选区状态有时还没同步完
BUBBLE_DELAY_MS = get_int('TTS_BUBBLE_DELAY_MS', 180)
# 多久没人理它就自己消失
BUBBLE_HIDE_MS = get_int('TTS_BUBBLE_HIDE_MS', 4000)
# 少于这么多字不弹（避免点一下光标就冒出来）
BUBBLE_MIN_CHARS = get_int('TTS_BUBBLE_MIN_CHARS', 2)

# ---------------------------- 语音输入（ASR，按住 Alt 说话） ----------------------------

# 总开关（托盘还能临时开关，偏好存 pref.json）
ASR_ENABLE = get_bool('ASR_ENABLE', True)
# 识别模型类型：sensevoice | streaming_zipformer（流式）
# ⚠ 封版只保留 sensevoice int8（models/asr/sensevoice-zh-int8，229MB）。
#    fp32 与 zipformer 目录已随封版清理移除，要切档得先补下模型文件
ASR_MODEL_TYPE = get('ASR_MODEL_TYPE', 'sensevoice').strip().lower()
if ASR_MODEL_TYPE not in ('sensevoice', 'streaming_zipformer'):
    ASR_MODEL_TYPE = 'sensevoice'
# SenseVoice 精度档：int8（228MB 文件 / 实测常驻 299MB）| fp32（894MB / 956MB，需自行补下）
# 实测 3 段中文音频两档输出逐字一致，量化没有可测损失 → 默认 int8
# 这个开关同时决定模型目录和文件名，切档只改这一项
ASR_VARIANT = get('ASR_VARIANT', 'int8').strip().lower()
if ASR_VARIANT not in ('fp32', 'int8'):
    ASR_VARIANT = 'int8'
# 模型目录（不填则按 类型+精度档 自动选；zipformer：encoder/decoder/joiner 四件套）
_DEFAULT_ASR_DIR = {
    ('sensevoice', 'fp32'): 'sensevoice-zh-fp32',
    ('sensevoice', 'int8'): 'sensevoice-zh-int8',
    ('streaming_zipformer', 'fp32'): 'zipformer-zh-streaming-int8',
    ('streaming_zipformer', 'int8'): 'zipformer-zh-streaming-int8',
}[(ASR_MODEL_TYPE, ASR_VARIANT)]
ASR_MODEL_DIR = get_path('ASR_MODEL_DIR', ROOT / 'models' / 'asr' / _DEFAULT_ASR_DIR)
ASR_THREADS = get_int('ASR_THREADS', 2)
# SenseVoice：逆文本正则化（数字转阿拉伯、加标点）
ASR_USE_ITN = get_bool('ASR_USE_ITN', True)
# 采样率：模型固定吃 16k，采集设备开不了 16k 时会自动回退设备默认率再做线性重采样
ASR_SAMPLE_RATE = get_int('ASR_SAMPLE_RATE', 16000)
# Alt 按下后等这么久没有其它键/鼠标键才认定是「按住说话」——Alt+右键/Alt+Tab 这类
# 组合键在宽限期内出现就取消 PTT，互不干扰
ASR_PTT_GRACE_MS = get_int('ASR_PTT_GRACE_MS', 250)
# 单次录音上限（秒）：到达即按松手处理，防止忘记松手一直录
ASR_MAX_S = get_int('ASR_MAX_S', 60)
# 识别出的文字少于这么多个字就不打字（误触保护）
ASR_MIN_CHARS = get_int('ASR_MIN_CHARS', 1)

# 识别引擎：local（本地 SenseVoice）| xfyun（讯飞听写，需凭据）
# 这里只是「没有 pref.json 时的初值」；托盘切换的结果存在 cache/pref.json
ASR_ENGINE = get('ASR_ENGINE', '').strip().lower()
if ASR_ENGINE in ('online', 'edge'):
    ASR_ENGINE = 'xfyun'
if ASR_ENGINE not in ('local', 'xfyun'):
    # 不填：配了讯飞凭据就默认在线（省 299MB 内存），没配就本地
    ASR_ENGINE = 'xfyun' if (XFYUN_APPID and XFYUN_API_KEY and XFYUN_API_SECRET) else 'local'


def voice_id(name=None):
    """female / male / 具体音色 id 都接受"""
    n = (name or VOICE_DEFAULT).strip()
    if n == 'female':
        return VOICE_FEMALE
    if n == 'male':
        return VOICE_MALE
    return n


def resolve_voice(name=None, kind=None):
    """任务里存哪个音色名 —— 按引擎决定。

    本地引擎必须落成 .bin 的 id（zf_001 / zm_029），因为缓存键、音色文件都按它走；
    在线引擎（讯飞 / Edge）则**保留逻辑名**（female / male），由引擎自己映射成
    vcn / Neural 名，这样切引擎时缓存与偏好都不会串味儿。

    kind 不传就按本进程启动时的 ENGINE；app 进程在托盘切完引擎后仍持着旧常量，
    所以那边要显式传目标引擎（见 config.voice_choices(kind) 的说明）。
    """
    kind = (kind or ENGINE).strip().lower()
    n = (name or VOICE_DEFAULT).strip()
    return voice_id(n) if kind == 'local' else n


def voice_choices(kind=None):
    """某个引擎下可选的音色 [(key, label)]，浮窗/托盘共用。

    ⚠ kind 默认为 None 时用本进程的 ENGINE。服务进程在切换引擎后会重启，
    读到的是新值；但 **app 进程不会重启**，它切完引擎后 config.ENGINE 还是旧的，
    所以 app 层必须显式传它自己认定的当前引擎，否则音色列表会错一整代。
    """
    kind = (kind or ENGINE).strip().lower()
    if kind == 'xfyun':
        return list(XFYUN_VOICES)
    if kind == 'edge':
        return list(EDGE_VOICES)
    return [('female', '女声'), ('male', '男声')]


# 引擎的显示名：托盘菜单、气泡通知、--engine status 共用一处，别各写一份
ENGINE_LABELS = {
    'local': '本地模型',
    'xfyun': '讯飞在线',
    'edge': 'Edge 在线',
}


def voice_ok(voice, kind=None):
    """这个音色名在该引擎下能不能直接用（切引擎后清理偏好用）。

    逻辑名 female / male 三家都认；具体音色名必须出现在该引擎的可选表里 ——
    zf_001 拿到 Edge 上去、zh-CN-XiaoxiaoNeural 拿到讯飞上去，都会被端点拒。
    """
    v = str(voice or '').strip()
    if not v:
        return False
    if v in ('female', 'male'):
        return True
    return v in {k for k, _ in voice_choices(kind)}


def voice_path(name=None):
    # ONNX 侧音色是裸 float32 的 .bin（[510,256]，522240 字节），与官方 HF 仓库同格式
    return VOICES_DIR / f'{voice_id(name)}.bin'
