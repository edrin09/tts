"""讯飞 WebAPI 公共件：签名鉴权 + 凭据/协议解析。

为什么单独一个文件
------------------
TTS（wss://tts-api.xfyun.cn/v2/tts）和听写（wss://iat-api.xfyun.cn/v2/iat）
用的是**同一套 HMAC-SHA256 签名**，只是 host / path 不同。签名算法抄在两边
迟早会改漏一处，所以收在这里。

签名规则（官方文档「接口鉴权」）
-------------------------------
    signature_origin = "host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature        = base64(hmac_sha256(api_secret, signature_origin))
    authorization    = base64(
        'api_key="{key}", algorithm="hmac-sha256",
         headers="host date request-line", signature="{signature}"')
    最终 URL = wss://{host}{path}?authorization=..&date=..&host=..

date 必须是 RFC1123 GMT（`Mon, 29 Oct 2026 08:00:00 GMT`），与服务器时差
超过 5 分钟会报 401，所以**不能缓存签名**，每次连接现算。
"""
import base64
import hashlib
import hmac
import time
from email.utils import formatdate
from urllib.parse import urlencode, urlparse, parse_qsl

from . import config


def auth_url(url, api_key, api_secret):
    """把裸的 wss/http url 变成带签名的最终 URL（保留原有 query）。"""
    u = urlparse(url)
    host = u.hostname
    if not host:
        raise ValueError(f'接口地址不合法: {url}')
    path = u.path or '/'
    date = formatdate(timeval=None, localtime=False, usegmt=True)

    origin = f'host: {host}\ndate: {date}\nGET {path} HTTP/1.1'
    sig = base64.b64encode(
        hmac.new(str(api_secret).encode('utf-8'), origin.encode('utf-8'),
                 hashlib.sha256).digest()).decode('ascii')
    auth_origin = (f'api_key="{api_key}", algorithm="hmac-sha256", '
                   f'headers="host date request-line", signature="{sig}"')

    q = dict(parse_qsl(u.query))
    q.update({'authorization': base64.b64encode(auth_origin.encode('utf-8')).decode('ascii'),
              'date': date, 'host': host})
    scheme = 'wss' if u.scheme in ('wss', 'https') else 'ws'
    return f'{scheme}://{host}{path}?{urlencode(q)}'


def host_path(url):
    u = urlparse(url)
    return f'{u.hostname}{u.path or "/"}'


def engine_label(kind):
    return {'local': '本地模型', 'xfyun': '讯飞在线'}.get(kind, kind)


def now_ms():
    return int(time.time() * 1000)


# ---------------------------- 配置解析 ----------------------------

def tts_creds():
    """(appid, api_key, api_secret, api_password)；没配全就抛人话错误。"""
    c = (config.XFYUN_APPID, config.XFYUN_API_KEY,
         config.XFYUN_API_SECRET, config.XFYUN_API_PASSWORD)
    if not c[0] or not c[1] or not c[2]:
        raise RuntimeError('讯飞凭据不全：请在 .env 里填 TTS_XFYUN_APPID / '
                           'TTS_XFYUN_API_KEY / TTS_XFYUN_API_SECRET')
    return c


def asr_creds():
    """听写优先用 ASR_XFYUN_*，没填就沿用 TTS_XFYUN_*（同一个应用即可）。"""
    appid = config.ASR_XFYUN_APPID or config.XFYUN_APPID
    key = config.ASR_XFYUN_API_KEY or config.XFYUN_API_KEY
    secret = config.ASR_XFYUN_API_SECRET or config.XFYUN_API_SECRET
    if not appid or not key or not secret:
        raise RuntimeError('讯飞听写凭据不全：填 ASR_XFYUN_* 或 TTS_XFYUN_* 任一套')
    return appid, key, secret


def tts_ready():
    return bool(config.XFYUN_APPID and config.XFYUN_API_KEY and config.XFYUN_API_SECRET)


def asr_ready():
    try:
        asr_creds()
        return True
    except Exception:
        return False
