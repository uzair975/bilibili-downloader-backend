from fastapi import APIRouter, Depends, Request, Query, HTTPException, status, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import httpx
from urllib.parse import unquote

try:
    from app.core.rate_limiter import check_rate_limit
    from app.services.extractor import BilibiliExtractor, ResolutionResult
    from app.services.headers import get_stream_download_headers, get_bilibili_api_headers
except ImportError:
    from backend.app.core.rate_limiter import check_rate_limit
    from backend.app.services.extractor import BilibiliExtractor, ResolutionResult
    from backend.app.services.headers import get_stream_download_headers, get_bilibili_api_headers


router = APIRouter()


class ResolveRequest(BaseModel):
    url: str = Field(..., description="Bilibili video URL, BV id, or b23.tv short link", min_length=3)


@router.get("/healthcheck", tags=["System"])
async def healthcheck():
    """Health check probe for container orchestrators (Render, Koyeb, Docker)."""
    return {
        "status": "healthy",
        "service": "BiliExtract Engine",
        "version": "1.0.0",
    }


@router.post("/resolve", response_model=ResolutionResult, dependencies=[Depends(check_rate_limit)], tags=["Extraction"])
async def resolve_video(payload: ResolveRequest, request: Request):
    """
    Parses Bilibili URL, extracts metadata, audio & video DASH streams, and subtitles.
    """
    base_url = str(request.base_url).rstrip("/")
    result = await BilibiliExtractor.resolve(payload.url, request_base_url=base_url)
    return result


import ipaddress
import urllib.parse

ALLOWED_STREAM_DOMAINS = (
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
    "bilibili.com",
    "akamaized.net",
    "biliapi.net",
)


def is_safe_bilibili_url(target_url: str) -> bool:
    """
    Strictly validates target URLs to prevent SSRF and Open Proxy abuse.
    Ensures URL only targets authentic Bilibili media/API CDNs and rejects private/cloud metadata IPs.
    """
    try:
        parsed = urllib.parse.urlparse(target_url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False

        # Reject loopback, private ranges, link-local, and cloud metadata (169.254.169.254)
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254"):
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass  # Domain name, check suffix

        # Match legitimate Bilibili domain suffixes
        if any(hostname == domain or hostname.endswith("." + domain) for domain in ALLOWED_STREAM_DOMAINS):
            return True

        # Handle upos-* CDN hostnames
        if "upos-" in hostname and any(d in hostname for d in ALLOWED_STREAM_DOMAINS):
            return True

        return False
    except Exception:
        return False


@router.api_route("/download", methods=["GET", "HEAD"], tags=["Stream Proxy"])
async def proxy_download(
    url: str = Query(..., description="Target Bilibili CDN stream URL"),
    filename: str = Query("bilibili_media.mp4", description="Custom download filename"),
    type: str = Query("video", description="Media type: video or audio"),
    request: Request = None,
):
    """
    Proxies Bilibili CDN media with spoofed Referer and User-Agent to bypass 403 Forbidden.
    Streams directly to the user's browser without storing media on server disk (< 256MB RAM friendly).
    Supports HTTP Range requests for instant streaming and pausing/resuming.
    """
    decoded_url = unquote(url)
    if not is_safe_bilibili_url(decoded_url):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized target domain or invalid stream URL.")

    headers = get_stream_download_headers()
    # Forward incoming Range header if client requested range
    client_range = request.headers.get("Range") if request else None
    if client_range:
        headers["Range"] = client_range

    client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    try:
        req = client.build_request("GET", decoded_url, headers=headers)
        upstream_resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to fetch upstream stream: {str(e)}")

    if upstream_resp.status_code not in (200, 206):
        await upstream_resp.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream returned HTTP {upstream_resp.status_code}",
        )

    # Sanitize and encode filename for RFC 5987 compliance (supports Chinese/Japanese/Unicode)
    import urllib.parse
    ascii_clean = "".join(c for c in filename if c.isascii() and c not in '\\/:"*?<>|').strip()
    if not ascii_clean:
        ascii_clean = "bilibili_image.jpg" if type == "image" else ("bilibili_video.mp4" if type == "video" else "bilibili_audio.mp3")
    utf8_encoded = urllib.parse.quote(filename)
    content_type = upstream_resp.headers.get("Content-Type") or ("video/mp4" if type == "video" else ("audio/mpeg" if type == "audio" else "image/jpeg"))

    resp_headers = {
        "Content-Disposition": f'attachment; filename="{ascii_clean}"; filename*=UTF-8\'\'{utf8_encoded}',
        "Accept-Ranges": "bytes",
    }
    if "Content-Length" in upstream_resp.headers:
        resp_headers["Content-Length"] = upstream_resp.headers["Content-Length"]
    if "Content-Range" in upstream_resp.headers:
        resp_headers["Content-Range"] = upstream_resp.headers["Content-Range"]

    async def stream_generator():
        try:
            async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_generator(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type,
    )


@router.get("/danmaku", tags=["Danmaku"])
async def proxy_danmaku(
    oid: int = Query(..., description="Video CID (oid)"),
    filename: str = Query("danmaku.xml", description="Download filename"),
):
    """
    Proxies and decompresses Bilibili Danmaku XML with spoofed headers.
    Prevents HTTP 412 / CORS issues when downloaded from the browser.
    """
    danmaku_url = f"https://api.bilibili.com/x/v1/dm/list.so?oid={oid}"
    headers = get_bilibili_api_headers()

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            resp = await client.get(danmaku_url, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to fetch danmaku: {str(e)}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Bilibili returned HTTP {resp.status_code}")

    xml_text = resp.text
    if not (xml_text.startswith("<?xml") or "<i>" in xml_text):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Invalid danmaku content received.")

    import urllib.parse
    ascii_clean = "".join(c for c in filename if c.isascii() and c not in '\\/:"*?<>|').strip() or "danmaku.xml"
    utf8_encoded = urllib.parse.quote(filename)

    resp_headers = {
        "Content-Disposition": f'attachment; filename="{ascii_clean}"; filename*=UTF-8\'\'{utf8_encoded}',
    }
    return Response(
        content=resp.content,
        media_type="application/xml; charset=utf-8",
        headers=resp_headers,
    )


def format_srt_timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def convert_bilibili_json_to_srt(sub_data: dict) -> str:
    body = sub_data.get("body", [])
    if not body:
        return ""
    srt_blocks = []
    for idx, item in enumerate(body, 1):
        start = float(item.get("from", 0.0))
        to = float(item.get("to", 0.0))
        content = item.get("content", "").strip()
        srt_blocks.append(f"{idx}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(to)}\n{content}\n")
    return "\n".join(srt_blocks)


@router.get("/subtitle", tags=["Subtitles"])
async def proxy_subtitle(
    url: str = Query(..., description="Target subtitle URL"),
    filename: str = Query("subtitle.srt", description="Custom download filename"),
    format: str = Query("srt", description="Output format: srt or json"),
):
    """
    Proxies Bilibili subtitle JSON and optionally converts it to standard SRT format.
    """
    decoded_url = unquote(url)
    if not is_safe_bilibili_url(decoded_url):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized target domain or invalid subtitle URL.")

    headers = get_bilibili_api_headers()
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            resp = await client.get(decoded_url, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to fetch subtitle: {str(e)}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Bilibili returned HTTP {resp.status_code}")

    import urllib.parse
    if format.lower() == "srt":
        try:
            sub_json = resp.json()
            srt_content = convert_bilibili_json_to_srt(sub_json)
            content_bytes = srt_content.encode("utf-8")
        except Exception:
            content_bytes = resp.content
        media_type = "text/plain; charset=utf-8"
        if not filename.endswith(".srt"):
            filename = filename.rsplit(".", 1)[0] + ".srt"
    else:
        content_bytes = resp.content
        media_type = "application/json; charset=utf-8"

    ascii_clean = "".join(c for c in filename if c.isascii() and c not in '\\/:"*?<>|').strip() or "subtitle.srt"
    utf8_encoded = urllib.parse.quote(filename)
    resp_headers = {
        "Content-Disposition": f'attachment; filename="{ascii_clean}"; filename*=UTF-8\'\'{utf8_encoded}',
    }
    return Response(
        content=content_bytes,
        media_type=media_type,
        headers=resp_headers,
    )
