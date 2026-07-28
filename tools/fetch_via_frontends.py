#!/usr/bin/env python3
"""Fetch a public YouTube lecture through privacy front-end proxy APIs.

This is a fallback for cloud runners whose IP addresses are blocked by YouTube.
The script tries Piped and Invidious instances, downloads the highest usable
stream at or below the requested height, saves captions/metadata, and muxes
separate audio/video streams with ffmpeg when needed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests


PIPED_APIS = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.syncpundit.io",
    "https://api-piped.mha.fi",
    "https://piped-api.garudalinux.org",
    "https://pipedapi.rivo.lol",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.drgns.space",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.nosebs.ru",
]

INVIDIOUS_FALLBACKS = [
    "yewtu.be",
    "inv.nadeko.net",
    "invidious.nerdvpn.de",
    "invidious.private.coffee",
    "invidious.fdn.fr",
    "invidious.privacyredirect.com",
    "inv.us.projectsegfau.lt",
    "invidious.projectsegfau.lt",
    "inv.tux.pizza",
    "invidious.jing.rocks",
]


@dataclass
class DownloadResult:
    provider: str
    instance: str
    video_file: str
    height: int | None
    title: str | None


class FetchError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def safe_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_height(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(\d{3,4})", str(value))
    return int(match.group(1)) if match else None


def extension_from_url_or_type(url: str, mime: str | None, default: str) -> str:
    if mime:
        lowered = mime.lower()
        if "webm" in lowered:
            return "webm"
        if "mp4" in lowered:
            return "mp4"
        if "mpegurl" in lowered or "m3u8" in lowered:
            return "m3u8"
        if "vtt" in lowered:
            return "vtt"
        if "json" in lowered:
            return "json"
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix and len(suffix) <= 8:
        return suffix
    return default


def stream_download(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    referer: str | None = None,
    max_bytes: int = 6 * 1024 * 1024 * 1024,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    temp = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, 4):
        try:
            with session.get(url, headers=headers, stream=True, timeout=(20, 120), allow_redirects=True) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise FetchError(f"stream too large: {length} bytes")
                total = 0
                with temp.open("wb") as fp:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"stream exceeded {max_bytes} bytes")
                        fp.write(chunk)
                        if total and total % (100 * 1024 * 1024) < len(chunk):
                            log(f"  downloaded {total / (1024**2):.1f} MiB -> {destination.name}")
                if total < 1024:
                    raise FetchError(f"downloaded only {total} bytes")
                temp.replace(destination)
                return
        except Exception as exc:
            temp.unlink(missing_ok=True)
            if attempt == 3:
                raise
            log(f"  download attempt {attempt} failed: {exc!r}; retrying")
            time.sleep(attempt * 3)


def ffmpeg_mux(video: Path, audio: Path | None, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video)]
    if audio:
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", str(output)]
    else:
        cmd += ["-c", "copy", str(output)]
    log("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def choose_piped_video(streams: list[dict[str, Any]], max_height: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    valid = []
    for item in streams:
        height = parse_height(item.get("quality") or item.get("qualityLabel") or item.get("height"))
        if not item.get("url") or height is None or height > max_height:
            continue
        valid.append((height, item))
    combined = [(height, item) for height, item in valid if not bool(item.get("videoOnly"))]
    video_only = [(height, item) for height, item in valid if bool(item.get("videoOnly"))]
    combined.sort(key=lambda pair: (pair[0], int(pair[1].get("bitrate") or 0)), reverse=True)
    video_only.sort(key=lambda pair: (pair[0], int(pair[1].get("bitrate") or 0)), reverse=True)
    best_video_only = video_only[0][1] if video_only else None
    best_combined = combined[0][1] if combined else None
    if best_video_only and parse_height(best_video_only.get("quality")) and parse_height(best_video_only.get("quality")) >= 480:
        return best_video_only, best_combined
    return best_combined or best_video_only, best_combined


def choose_audio(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [item for item in streams if item.get("url")]
    valid.sort(key=lambda item: int(item.get("bitrate") or 0), reverse=True)
    return valid[0] if valid else None


def download_captions(
    session: requests.Session,
    captions: Iterable[dict[str, Any]],
    source_dir: Path,
    referer: str,
    prefix: str,
) -> None:
    items = list(captions)
    items.sort(key=lambda item: (0 if str(item.get("code") or item.get("languageCode") or "").lower().startswith("ko") else 1))
    for index, item in enumerate(items):
        url = item.get("url")
        if not url:
            continue
        code = str(item.get("code") or item.get("languageCode") or f"track{index}")
        name = re.sub(r"[^0-9A-Za-z._-]+", "_", code)
        mime = item.get("mimeType") or item.get("type")
        ext = extension_from_url_or_type(str(url), str(mime) if mime else None, "vtt")
        destination = source_dir / f"{prefix}.{name}.{ext}"
        try:
            stream_download(session, str(url), destination, referer=referer, max_bytes=50 * 1024 * 1024)
        except Exception as exc:
            log(f"  caption failed {code}: {exc!r}")


def try_piped(
    session: requests.Session,
    video_id: str,
    work_dir: Path,
    source_dir: Path,
    max_height: int,
    diagnostics: list[dict[str, Any]],
) -> DownloadResult | None:
    for base in PIPED_APIS:
        endpoint = f"{base.rstrip('/')}/streams/{video_id}"
        log(f"Piped: {endpoint}")
        record: dict[str, Any] = {"provider": "piped", "instance": base, "endpoint": endpoint}
        try:
            response = session.get(endpoint, timeout=(12, 45))
            record.update(status=response.status_code, bytes=len(response.content))
            response.raise_for_status()
            data = response.json()
            if data.get("error") or not data.get("videoStreams"):
                raise FetchError(str(data.get("error") or "no videoStreams"))
            safe_json(source_dir / "piped_streams.json", data)
            download_captions(session, data.get("subtitles") or [], source_dir, base, "piped-caption")

            chosen, combined_fallback = choose_piped_video(data.get("videoStreams") or [], max_height)
            if not chosen:
                raise FetchError("no usable video stream")
            height = parse_height(chosen.get("quality") or chosen.get("qualityLabel") or chosen.get("height"))
            chosen_url = str(chosen["url"])
            if chosen_url.startswith("/"):
                chosen_url = urljoin(base + "/", chosen_url)
            video_only = bool(chosen.get("videoOnly"))
            video_ext = extension_from_url_or_type(chosen_url, chosen.get("mimeType") or chosen.get("format"), "mp4")
            raw_video = work_dir / f"piped-video.{video_ext}"
            stream_download(session, chosen_url, raw_video, referer=base)

            raw_audio: Path | None = None
            if video_only:
                audio = choose_audio(data.get("audioStreams") or [])
                if not audio:
                    if combined_fallback and combined_fallback.get("url"):
                        chosen = combined_fallback
                        height = parse_height(chosen.get("quality") or chosen.get("qualityLabel"))
                        chosen_url = str(chosen["url"])
                        if chosen_url.startswith("/"):
                            chosen_url = urljoin(base + "/", chosen_url)
                        raw_video.unlink(missing_ok=True)
                        video_ext = extension_from_url_or_type(chosen_url, chosen.get("mimeType") or chosen.get("format"), "mp4")
                        raw_video = work_dir / f"piped-combined.{video_ext}"
                        stream_download(session, chosen_url, raw_video, referer=base)
                        video_only = False
                    else:
                        raise FetchError("video-only stream had no audio stream")
                else:
                    audio_url = str(audio["url"])
                    if audio_url.startswith("/"):
                        audio_url = urljoin(base + "/", audio_url)
                    audio_ext = extension_from_url_or_type(audio_url, audio.get("mimeType") or audio.get("format"), "m4a")
                    raw_audio = work_dir / f"piped-audio.{audio_ext}"
                    stream_download(session, audio_url, raw_audio, referer=base)

            output = work_dir / "lecture.mkv"
            ffmpeg_mux(raw_video, raw_audio if video_only else None, output)
            record.update(success=True, height=height, selected=chosen)
            diagnostics.append(record)
            safe_json(source_dir / "frontend_fetch_diagnostics.json", diagnostics)
            return DownloadResult("piped", base, str(output), height, data.get("title"))
        except Exception as exc:
            record["error"] = repr(exc)
            diagnostics.append(record)
            log(f"  failed: {exc!r}")
            for path in work_dir.glob("piped-*"):
                path.unlink(missing_ok=True)
    return None


def get_invidious_instances(session: requests.Session) -> list[str]:
    domains: list[str] = []
    try:
        response = session.get("https://api.invidious.io/instances.json", timeout=(15, 35))
        response.raise_for_status()
        for domain, details in response.json():
            if not isinstance(details, dict):
                continue
            if details.get("type") != "https" or details.get("api") is not True:
                continue
            domains.append(domain)
    except Exception as exc:
        log(f"instance registry failed: {exc!r}")
    for domain in INVIDIOUS_FALLBACKS:
        if domain not in domains:
            domains.append(domain)
    return domains[:45]


def quality_key(item: dict[str, Any], max_height: int) -> tuple[int, int] | None:
    height = parse_height(item.get("qualityLabel") or item.get("quality") or item.get("height"))
    if height is None or height > max_height:
        return None
    return height, int(item.get("bitrate") or 0)


def local_invidious_url(base: str, video_id: str, item: dict[str, Any]) -> str:
    itag = item.get("itag")
    if itag is not None:
        return f"{base.rstrip('/')}/latest_version?id={video_id}&itag={itag}&local=true"
    return urljoin(base + "/", str(item.get("url") or ""))


def try_invidious(
    session: requests.Session,
    video_id: str,
    work_dir: Path,
    source_dir: Path,
    max_height: int,
    diagnostics: list[dict[str, Any]],
) -> DownloadResult | None:
    for domain in get_invidious_instances(session):
        base = f"https://{domain}"
        endpoint = f"{base}/api/v1/videos/{video_id}?local=true"
        log(f"Invidious: {endpoint}")
        record: dict[str, Any] = {"provider": "invidious", "instance": base, "endpoint": endpoint}
        try:
            response = session.get(endpoint, timeout=(12, 45))
            record.update(status=response.status_code, bytes=len(response.content))
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise FetchError(str(data["error"]))
            safe_json(source_dir / "invidious_video.json", data)
            captions = []
            for item in data.get("captions") or []:
                copied = dict(item)
                if copied.get("url"):
                    copied["url"] = urljoin(base + "/", str(copied["url"]))
                captions.append(copied)
            download_captions(session, captions, source_dir, base, "invidious-caption")

            format_streams = []
            for item in data.get("formatStreams") or []:
                key = quality_key(item, max_height)
                if key:
                    format_streams.append((key, item))
            format_streams.sort(key=lambda pair: pair[0], reverse=True)

            adaptive_video = []
            adaptive_audio = []
            for item in data.get("adaptiveFormats") or []:
                mime = str(item.get("type") or "").lower()
                if mime.startswith("video/"):
                    key = quality_key(item, max_height)
                    if key:
                        adaptive_video.append((key, item))
                elif mime.startswith("audio/"):
                    adaptive_audio.append((int(item.get("bitrate") or 0), item))
            adaptive_video.sort(key=lambda pair: pair[0], reverse=True)
            adaptive_audio.sort(key=lambda pair: pair[0], reverse=True)

            raw_audio: Path | None = None
            if adaptive_video and adaptive_video[0][0][0] >= 480 and adaptive_audio:
                height, chosen = adaptive_video[0]
                height_value = height[0]
                video_url = local_invidious_url(base, video_id, chosen)
                video_ext = extension_from_url_or_type(video_url, chosen.get("type"), "mp4")
                raw_video = work_dir / f"invidious-video.{video_ext}"
                stream_download(session, video_url, raw_video, referer=base)
                audio = adaptive_audio[0][1]
                audio_url = local_invidious_url(base, video_id, audio)
                audio_ext = extension_from_url_or_type(audio_url, audio.get("type"), "m4a")
                raw_audio = work_dir / f"invidious-audio.{audio_ext}"
                stream_download(session, audio_url, raw_audio, referer=base)
            elif format_streams:
                _, chosen = format_streams[0]
                height_value = quality_key(chosen, max_height)[0]
                video_url = local_invidious_url(base, video_id, chosen)
                video_ext = extension_from_url_or_type(video_url, chosen.get("type") or chosen.get("container"), "mp4")
                raw_video = work_dir / f"invidious-combined.{video_ext}"
                stream_download(session, video_url, raw_video, referer=base)
            else:
                raise FetchError("no usable formatStreams/adaptiveFormats")

            output = work_dir / "lecture.mkv"
            ffmpeg_mux(raw_video, raw_audio, output)
            record.update(success=True, height=height_value, selected=chosen)
            diagnostics.append(record)
            safe_json(source_dir / "frontend_fetch_diagnostics.json", diagnostics)
            return DownloadResult("invidious", base, str(output), height_value, data.get("title"))
        except Exception as exc:
            record["error"] = repr(exc)
            diagnostics.append(record)
            log(f"  failed: {exc!r}")
            for path in work_dir.glob("invidious-*"):
                path.unlink(missing_ok=True)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--max-height", type=int, default=720)
    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    source_dir = args.source_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        }
    )
    diagnostics: list[dict[str, Any]] = []

    result = try_piped(session, args.video_id, work_dir, source_dir, args.max_height, diagnostics)
    if result is None:
        result = try_invidious(session, args.video_id, work_dir, source_dir, args.max_height, diagnostics)
    safe_json(source_dir / "frontend_fetch_diagnostics.json", diagnostics)
    if result is None:
        raise FetchError("all Piped and Invidious instances failed")
    safe_json(source_dir / "frontend_fetch_result.json", result.__dict__)
    log(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    print(result.video_file)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr, flush=True)
        raise
