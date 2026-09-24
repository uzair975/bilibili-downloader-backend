import re
import json
import urllib.parse
from typing import Dict, Any, List, Optional
import httpx
from pydantic import BaseModel
from backend.app.config import settings
from backend.app.services.headers import get_bilibili_api_headers
from backend.app.core.exceptions import InvalidUrlException, BilibiliApiException, StreamNotFoundException


class VideoStreamOption(BaseModel):
    quality: int
    label: str
    resolution: str
    fps: Optional[int | float | str] = 30
    codec: str
    stream_url: str
    download_url: str
    size_bytes: Optional[int] = None
    size_formatted: Optional[str] = None
    has_audio: bool = False


class MergedStreamOption(BaseModel):
    """Combined video+audio stream (legacy MP4 with sound built-in)."""
    quality: int
    label: str
    stream_url: str
    download_url: str
    size_bytes: Optional[int] = None
    size_formatted: Optional[str] = None


class AudioStreamOption(BaseModel):
    id: int
    label: str
    bitrate: str
    codec: str
    stream_url: str
    download_url: str
    size_bytes: Optional[int] = None
    size_formatted: Optional[str] = None


class SubtitleTrack(BaseModel):
    id: int
    lang: str
    label: str
    url: str


class ResolutionResult(BaseModel):
    bvid: str
    aid: int
    cid: int
    title: str
    description: str
    thumbnail: str
    duration_seconds: int
    duration_formatted: str
    author_name: str
    author_avatar: str
    views: int
    danmaku_count: int
    danmaku_url: str
    videos: List[VideoStreamOption]
    audios: List[AudioStreamOption]
    merged: List[MergedStreamOption]
    subtitles: List[SubtitleTrack]


QUALITY_MAP = {
    127: "8K Ultra HD",
    120: "4K Ultra HD",
    116: "1080P 60FPS",
    112: "1080P High Bitrate",
    80: "1080P Full HD",
    74: "720P 60FPS",
    64: "720P HD",
    32: "480P Standard",
    16: "360P Smooth",
}

AUDIO_QUALITY_MAP = {
    30280: ("320 kbps", "Ultra High Fidelity"),
    30232: ("192 kbps", "High Quality"),
    30216: ("128 kbps", "Standard Quality"),
    30200: ("64 kbps", "Low Bitrate"),
}


def format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(bytes_count: Optional[int]) -> str:
    if not bytes_count:
        return "Auto"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} TB"


def normalize_bilibili_image_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


class BilibiliExtractor:
    BV_REGEX = re.compile(r"(BV[a-zA-Z0-9]{10})", re.IGNORECASE)
    AV_REGEX = re.compile(r"(?:av|aid=?)(\d+)", re.IGNORECASE)
    B23_REGEX = re.compile(r"b23\.tv/([a-zA-Z0-9]+)", re.IGNORECASE)

    @classmethod
    async def extract_identifier(cls, raw_input: str) -> Dict[str, Any]:
        """Resolves input string or shortlink to bvid or aid."""
        clean_input = raw_input.strip()

        # Check for b23.tv shortlink
        if "b23.tv" in clean_input:
            async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True) as client:
                try:
                    if not clean_input.startswith("http"):
                        clean_input = "https://" + clean_input
                    resp = await client.head(clean_input, headers=get_bilibili_api_headers())
                    clean_input = str(resp.url)
                except Exception:
                    pass

        # Match BV ID
        bv_match = cls.BV_REGEX.search(clean_input)
        if bv_match:
            return {"type": "bv", "id": bv_match.group(1)}

        # Match AV ID
        av_match = cls.AV_REGEX.search(clean_input)
        if av_match:
            return {"type": "av", "id": av_match.group(1)}

        raise InvalidUrlException(
            "Could not detect a valid Bilibili video ID (BVxxxxxx or avxxxxxx). Please check the link."
        )

    @classmethod
    async def resolve(cls, raw_url: str, request_base_url: str = "") -> ResolutionResult:
        identifier = await cls.extract_identifier(raw_url)
        bvid_target = identifier["id"] if identifier["type"] == "bv" else None

        # Base download proxy prefix
        proxy_endpoint = f"{request_base_url}/api/v1/download" if request_base_url else "/api/v1/download"
        headers = get_bilibili_api_headers()

        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True) as client:
            video_url = (
                f"https://www.bilibili.com/video/{bvid_target}"
                if bvid_target
                else f"https://www.bilibili.com/video/av{identifier['id']}"
            )

            # Strategy 1: Extract embedded state & playinfo directly from web HTML (Bypasses API rate limits)
            try:
                page_resp = await client.get(video_url, headers=headers)
                if page_resp.status_code == 200:
                    html = page_resp.text
                    result = await cls._parse_from_html(html, proxy_endpoint, client)
                    if result:
                        return result
            except Exception:
                pass

            # Strategy 2: Upstream REST API fallback
            return await cls._resolve_via_api(client, identifier, proxy_endpoint, headers)

    @classmethod
    async def _parse_from_html(cls, html: str, proxy_endpoint: str, client: httpx.AsyncClient) -> Optional[ResolutionResult]:
        decoder = json.JSONDecoder()
        state_idx = html.find("window.__INITIAL_STATE__")
        play_idx = html.find("window.__playinfo__")

        if state_idx == -1:
            return None

        # Decode initial state
        try:
            brace_idx = html.find("{", state_idx)
            state_data, _ = decoder.raw_decode(html[brace_idx:])
        except Exception:
            return None

        vdata = state_data.get("videoData", {})
        if not vdata:
            return None

        bvid = vdata.get("bvid", "")
        aid = vdata.get("aid", 0)
        cid = vdata.get("cid", 0)
        title = vdata.get("title", "Bilibili Video")
        desc = vdata.get("desc", "")
        pic = vdata.get("pic", "")
        duration = vdata.get("duration", 0)
        owner = vdata.get("owner", {})
        stat = vdata.get("stat", {})

        # Decode play info if available
        play_data = {}
        if play_idx != -1:
            try:
                p_brace = html.find("{", play_idx)
                play_data, _ = decoder.raw_decode(html[p_brace:])
            except Exception:
                pass

        dash = play_data.get("data", {}).get("dash", {})
        durl = play_data.get("data", {}).get("durl", [])

        # Query high-definition DASH streams with fnval=4048 and try_look=1 to unlock 1080P Full HD
        headers = get_bilibili_api_headers()
        playurl_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=120&fnval=4048&fnver=0&fourk=1&try_look=1"
        try:
            p_resp = await client.get(playurl_api, headers=headers)
            p_json = p_resp.json()
            api_dash = p_json.get("data", {}).get("dash")
            if api_dash and api_dash.get("video"):
                dash = api_dash
        except Exception:
            pass

        videos, audios = cls._build_stream_options(dash, durl, title, duration, proxy_endpoint)

        # Subtitles
        clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
        subtitles: List[SubtitleTrack] = []
        sub_list = vdata.get("subtitle", {}).get("list", [])
        for sub in sub_list:
            sub_url = sub.get("subtitle_url", "")
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            sub_fname = urllib.parse.quote(f"{clean_title}_{sub.get('lan', 'sub')}.srt")
            encoded_sub_url = urllib.parse.quote(sub_url, safe='')
            proxied_sub_url = f"{proxy_endpoint.replace('/download', '/subtitle')}?url={encoded_sub_url}&filename={sub_fname}&format=srt"
            subtitles.append(
                SubtitleTrack(
                    id=sub.get("id", 0),
                    lang=sub.get("lan", ""),
                    label=sub.get("lan_doc", "Subtitle"),
                    url=proxied_sub_url,
                )
            )

        encoded_dm_fname = urllib.parse.quote(f"{clean_title}_danmaku.xml")
        danmaku_url = f"{proxy_endpoint.replace('/download', '/danmaku')}?oid={cid}&filename={encoded_dm_fname}"

        # Fetch combined video+audio streams (legacy fnval=0)
        merged = await cls._fetch_merged_streams(client, bvid, cid, title, proxy_endpoint)

        return ResolutionResult(
            bvid=bvid,
            aid=aid,
            cid=cid,
            title=title,
            description=desc[:250] + ("..." if len(desc) > 250 else ""),
            thumbnail=normalize_bilibili_image_url(pic),
            duration_seconds=duration,
            duration_formatted=format_duration(duration),
            author_name=owner.get("name", "Unknown Creator"),
            author_avatar=normalize_bilibili_image_url(owner.get("face", "")),
            views=stat.get("view", 0),
            danmaku_count=stat.get("danmaku", 0),
            danmaku_url=danmaku_url,
            videos=videos,
            audios=audios,
            merged=merged,
            subtitles=subtitles,
        )

    @classmethod
    def _build_stream_options(
        cls, dash: Dict[str, Any], durl: List[Any], title: str, duration: int, proxy_endpoint: str
    ):
        videos: List[VideoStreamOption] = []
        audios: List[AudioStreamOption] = []

        if dash:
            # Video DASH streams
            raw_videos = dash.get("video", [])
            seen_qualities = set()
            for v in raw_videos:
                qid = v.get("id", 64)
                codecid = v.get("codecid", 7)
                key = (qid, codecid)
                if key in seen_qualities:
                    continue
                seen_qualities.add(key)

                base_url = v.get("baseUrl") or (v.get("backupUrl", [""])[0] if v.get("backupUrl") else "")
                if not base_url:
                    continue

                clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
                v_filename = f"{clean_title}_{qid}p.mp4"
                encoded_url = urllib.parse.quote(base_url, safe='')
                encoded_fname = urllib.parse.quote(v_filename)
                download_url = f"{proxy_endpoint}?url={encoded_url}&filename={encoded_fname}&type=video"

                bandwidth = v.get("bandwidth", 0)
                approx_size = int((bandwidth * duration) / 8) if duration and bandwidth else None

                raw_fps = v.get("frameRate", 30)
                try:
                    parsed_fps = int(round(float(raw_fps)))
                except (ValueError, TypeError):
                    parsed_fps = 30

                videos.append(
                    VideoStreamOption(
                        quality=qid,
                        label=QUALITY_MAP.get(qid, f"{qid}P Video") + " ⚠️ Video Only",
                        resolution=f"{v.get('width', 1920)}x{v.get('height', 1080)}",
                        fps=parsed_fps,
                        codec="H.264 / AVC" if codecid == 7 else "HEVC / H.265" if codecid == 12 else "AV1",
                        stream_url=base_url,
                        download_url=download_url,
                        size_bytes=approx_size,
                        size_formatted=format_bytes(approx_size),
                        has_audio=False,
                    )
                )

            # Audio DASH streams
            raw_audios = dash.get("audio", [])
            seen_audio_ids = set()
            for a in raw_audios:
                aid_level = a.get("id", 30280)
                if aid_level in seen_audio_ids:
                    continue
                seen_audio_ids.add(aid_level)

                base_url = a.get("baseUrl") or (a.get("backupUrl", [""])[0] if a.get("backupUrl") else "")
                if not base_url:
                    continue

                bitrate, label = AUDIO_QUALITY_MAP.get(aid_level, ("128 kbps", "Standard Audio"))
                clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
                a_filename = f"{clean_title}_{bitrate}.mp3"
                encoded_url = urllib.parse.quote(base_url, safe='')
                encoded_fname = urllib.parse.quote(a_filename)
                download_url = f"{proxy_endpoint}?url={encoded_url}&filename={encoded_fname}&type=audio"

                bandwidth = a.get("bandwidth", 0)
                approx_size = int((bandwidth * duration) / 8) if duration and bandwidth else None

                audios.append(
                    AudioStreamOption(
                        id=aid_level,
                        label=label,
                        bitrate=bitrate,
                        codec=a.get("codecs", "mp4a.40.2"),
                        stream_url=base_url,
                        download_url=download_url,
                        size_bytes=approx_size,
                        size_formatted=format_bytes(approx_size),
                    )
                )

        elif durl:
            for idx, d in enumerate(durl):
                stream_url = d.get("url", "")
                clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
                v_filename = f"{clean_title}_part{idx+1}.mp4"
                encoded_url = urllib.parse.quote(stream_url, safe='')
                encoded_fname = urllib.parse.quote(v_filename)
                download_url = f"{proxy_endpoint}?url={encoded_url}&filename={encoded_fname}&type=video"
                size = d.get("size")
                videos.append(
                    VideoStreamOption(
                        quality=64,
                        label="Direct MP4 (Standard)",
                        resolution="Standard",
                        fps=30,
                        codec="H.264",
                        stream_url=stream_url,
                        download_url=download_url,
                        size_bytes=size,
                        size_formatted=format_bytes(size),
                    )
                )

        videos.sort(key=lambda x: x.quality, reverse=True)
        audios.sort(key=lambda x: x.id, reverse=True)
        return videos, audios

    @classmethod
    async def _fetch_merged_streams(
        cls, client: httpx.AsyncClient, bvid: str, cid: int, title: str, proxy_endpoint: str
    ) -> List[MergedStreamOption]:
        """Fetch legacy combined video+audio MP4 streams via fnval=0."""
        merged: List[MergedStreamOption] = []
        headers = get_bilibili_api_headers()

        # Try multiple quality levels descending: 80=1080p, 64=720p, 32=480p, 16=360p
        for qn in [80, 64, 32, 16]:
            api_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=0&fnver=0"
            try:
                resp = await client.get(api_url, headers=headers)
                data = resp.json()
                if data.get("code") != 0:
                    continue

                achieved_qn = data.get("data", {}).get("quality", qn)
                durl_list = data.get("data", {}).get("durl", [])
                if not durl_list:
                    continue

                d = durl_list[0]
                stream_url = d.get("url", "")
                if not stream_url:
                    continue

                # Avoid duplicates if Bilibili returned same quality for different qn requests
                if any(m.quality == achieved_qn for m in merged):
                    continue

                size = d.get("size")
                clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
                fname = f"{clean_title}_{achieved_qn}p_merged.mp4"
                encoded_url = urllib.parse.quote(stream_url, safe='')
                encoded_fname = urllib.parse.quote(fname)
                download_url = f"{proxy_endpoint}?url={encoded_url}&filename={encoded_fname}&type=video"

                merged.append(
                    MergedStreamOption(
                        quality=achieved_qn,
                        label=QUALITY_MAP.get(achieved_qn, f"{achieved_qn}P") + " (Video + Audio)",
                        stream_url=stream_url,
                        download_url=download_url,
                        size_bytes=size,
                        size_formatted=format_bytes(size),
                    )
                )
            except Exception:
                continue

        merged.sort(key=lambda x: x.quality, reverse=True)
        return merged

    @classmethod
    async def _resolve_via_api(
        cls, client: httpx.AsyncClient, identifier: Dict[str, Any], proxy_endpoint: str, headers: Dict[str, str]
    ) -> ResolutionResult:
        if identifier["type"] == "bv":
            info_api = f"https://api.bilibili.com/x/web-interface/view?bvid={identifier['id']}"
        else:
            info_api = f"https://api.bilibili.com/x/web-interface/view?aid={identifier['id']}"

        try:
            info_resp = await client.get(info_api, headers=headers)
            info_data = info_resp.json()
        except Exception as e:
            raise BilibiliApiException(f"Error contacting Bilibili API: {str(e)}")

        if info_data.get("code") != 0 or not info_data.get("data"):
            msg = info_data.get("message", "Video not found or access restricted.")
            raise BilibiliApiException(f"Bilibili API error: {msg}")

        data = info_data["data"]
        bvid = data.get("bvid", "")
        aid = data.get("aid", 0)
        cid = data.get("cid", 0)
        title = data.get("title", "Bilibili Video")
        desc = data.get("desc", "")
        pic = data.get("pic", "")
        duration = data.get("duration", 0)
        owner = data.get("owner", {})
        stat = data.get("stat", {})

        playurl_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=120&fnval=4048&fnver=0&fourk=1&try_look=1"
        try:
            play_resp = await client.get(playurl_api, headers=headers)
            play_data = play_resp.json()
        except Exception as e:
            raise BilibiliApiException(f"Error fetching stream data: {str(e)}")

        dash = play_data.get("data", {}).get("dash", {})
        durl = play_data.get("data", {}).get("durl", [])
        videos, audios = cls._build_stream_options(dash, durl, title, duration, proxy_endpoint)

        # Fetch combined video+audio streams
        merged = await cls._fetch_merged_streams(client, bvid, cid, title, proxy_endpoint)

        clean_title = re.sub(r'[\/*?:"<>|]', "_", title)[:50]
        encoded_dm_fname = urllib.parse.quote(f"{clean_title}_danmaku.xml")
        danmaku_url = f"{proxy_endpoint.replace('/download', '/danmaku')}?oid={cid}&filename={encoded_dm_fname}"

        return ResolutionResult(
            bvid=bvid,
            aid=aid,
            cid=cid,
            title=title,
            description=desc[:250] + ("..." if len(desc) > 250 else ""),
            thumbnail=normalize_bilibili_image_url(pic),
            duration_seconds=duration,
            duration_formatted=format_duration(duration),
            author_name=owner.get("name", "Unknown Creator"),
            author_avatar=normalize_bilibili_image_url(owner.get("face", "")),
            views=stat.get("view", 0),
            danmaku_count=stat.get("danmaku", 0),
            danmaku_url=danmaku_url,
            videos=videos,
            audios=audios,
            merged=merged,
            subtitles=[],
        )
