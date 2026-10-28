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

# 执行引擎换成了 ONNX：不再有 .pth 权重，模型就是这个 onnx 文件
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
# 0 = 关闭（永远按 first_chunk 切，首字最快）；设成 40 就还原「用户验收那版」的整句听感
NOSPLIT_CHARS = get_int('TTS_NOSPLIT_CHARS', 0)
CHUNK_MAX = get_int('TTS_CHUNK_MAX', 60)
CHUNK_GAP_MS = get_int('TTS_CHUNK_GAP_MS', 50)

HOST = get('TTS_HOST', '127.0.0.1')
PORT = get_int('TTS_PORT', 9528)
THREADS = get_int('TTS_THREADS', 8)
WARMUP = get_bool('TTS_WARMUP', True)
WARMUP_TEXT = get('TTS_WARMUP_TEXT', '预热。')

ZH_PATCH = get_bool('TTS_ZH_PATCH', True)

# 合成引擎：local = 本地 ONNX（~820MB，离线）| online = Edge TTS（~100MB，需联网）
# 托盘/ run.py --engine 切换，改的是 .env 里这一项，重启服务进程后生效
ENGINE = get('TTS_ENGINE', 'local').strip().lower()
if ENGINE not in ('local', 'online'):
    ENGINE = 'local'

# ---------------------------- 阶段一：服务 / 播放 / 倍速 ----------------------------

# 日志体系已拆除（无 LOG_DIR、不落盘）；见 run.py / service/server.py 的 disable 说明

# 用户看到的倍速档位；内部 1.0× 对应的真实 speed 是 SPEED_BASE(1.1)
RATE_DEFAULT = get_float('TTS_RATE_DEFAULT', 1.0)
RATES = [float(x) for x in str(get('TTS_RATES', '0.75,1.0,1.25,1.5')).split(',') if x.strip()]

# 播放器向前预取的缓冲秒数（ROADMAP 要求 ≥3s）
PREFETCH_S = get_float('TTS_PREFETCH_S', 3.0)
# 预取高水位：低于它就继续拉下一块（只按 3s 预取会让 60 字块来不及合成）
BUFFER_HIGH_S = get_float('TTS_BUFFER_HIGH_S', 20.0)
# 起播门槛上限（秒）：预计断流时先攒缓冲再起播，但最多多等这么久
START_GATE_MAX_S = get_float('TTS_START_GATE_MAX_S', 2.5)

# 缓存/任务清理
CACHE_MAX_MB = get_int('TTS_CACHE_MAX_MB', 200)
CACHE_SALT = get('TTS_CACHE_SALT', 'v1-zf001-zm029')
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

# 在线识别通道（留口子，暂无默认服务商）：填了 URL 托盘才出现「在线」选项。
# 约定：POST {url}，header `Authorization: Bearer {key}`，JSON body
# {"audio": "<wav 的 base64>"}，响应 JSON {"text": "..."}
ASR_ONLINE_URL = (get('ASR_ONLINE_URL', '') or '').strip()
ASR_ONLINE_KEY = (get('ASR_ONLINE_KEY', '') or '').strip()


def voice_id(name=None):
    """female / male / 具体音色 id 都接受"""
    n = (name or VOICE_DEFAULT).strip()
    if n == 'female':
        return VOICE_FEMALE
    if n == 'male':
        return VOICE_MALE
    return n


def voice_path(name=None):
    # ONNX 侧音色是裸 float32 的 .bin（[510,256]，522240 字节），与官方 HF 仓库同格式
    return VOICES_DIR / f'{voice_id(name)}.bin'
