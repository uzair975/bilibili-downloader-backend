import random
from typing import Dict

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


import uuid
import time
from backend.app.config import settings


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


def get_bilibili_cookie_header() -> str:
    """Builds Bilibili cookie header supporting custom SESSDATA or realistic guest buvid3."""
    cookie_parts = []
    
    if settings.BILIBILI_COOKIE:
        return settings.BILIBILI_COOKIE
        
    if settings.BILIBILI_SESSDATA:
        cookie_parts.append(f"SESSDATA={settings.BILIBILI_SESSDATA}")

    # Guest session identifiers
    fake_buvid = f"{uuid.uuid4()}infoc"
    cookie_parts.append(f"buvid3={fake_buvid}")
    cookie_parts.append(f"b_nut={int(time.time())}")
    cookie_parts.append("CURRENT_FNVAL=4048")
    cookie_parts.append("_uuid=guest")
    
    return "; ".join(cookie_parts)


def get_bilibili_api_headers() -> Dict[str, str]:
    """Headers for querying public Bilibili REST APIs."""
    headers = {
        "User-Agent": get_random_ua(),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie": get_bilibili_cookie_header(),
    }
    return headers


def get_stream_download_headers() -> Dict[str, str]:
    """Crucial headers for downloading media chunks from *.bilivideo.com CDNs to bypass 403 Forbidden."""
    return {
        "User-Agent": get_random_ua(),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Encoding": "identity;q=1, *;q=0",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
