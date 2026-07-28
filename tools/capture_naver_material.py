#!/usr/bin/env python3
"""Capture the creator-linked Naver post, attachments, and large source images.

The script combines a real Chromium session with same-session HTTP retrieval so
that dynamically rendered SmartEditor content and attachment URLs are preserved.
It saves page HTML/text/screenshots, network metadata, PDFs/documents, and large
images while deduplicating identical payloads by SHA-256.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import io
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from PIL import Image
from playwright.async_api import BrowserContext, Page, Response, async_playwright


PAGE_URLS = (
    "https://blog.naver.com/{blog_id}/{log_no}",
    "https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}",
    "https://m.blog.naver.com/{blog_id}/{log_no}",
    "https://m.blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}",
    "https://rss.blog.naver.com/{blog_id}.xml",
)

INTERESTING_MARKERS = (
    ".pdf",
    "attach",
    "download",
    "blogfiles",
    "postfiles",
    "mfiles",
    "pstatic.net",
    "mblogthumb",
    "smarteditor",
    "PostView",
    "blog.naver.com",
)


def safe_name(value: str, fallback: str = "asset", limit: int = 180) -> str:
    value = unquote(value)
    value = re.sub(r"[^0-9A-Za-z가-힣._ -]+", "_", value).strip(" ._")
    return (value or fallback)[:limit]


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def decode_repeated(value: str) -> str:
    value = html.unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def choose_extension(content_type: str, url: str, content_disposition: str) -> str:
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.I)
    plain = re.search(r'filename="?([^";]+)', content_disposition, re.I)
    candidate = unquote(encoded.group(1)) if encoded else (plain.group(1) if plain else "")
    suffix = Path(candidate).suffix or Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 10:
        return suffix.lower()
    normalized = content_type.split(";", 1)[0].strip().lower()
    overrides = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/msword": ".doc",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "text/html": ".html",
        "application/json": ".json",
    }
    return overrides.get(normalized) or (mimetypes.guess_extension(normalized) or ".bin")


def content_filename(url: str, disposition: str, index: int, extension: str) -> str:
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    plain = re.search(r'filename="?([^";]+)', disposition, re.I)
    candidate = unquote(encoded.group(1)) if encoded else (plain.group(1) if plain else "")
    if not candidate:
        candidate = Path(urlparse(url).path).name
    candidate = safe_name(candidate, f"asset_{index:04d}")
    if not Path(candidate).suffix:
        candidate += extension
    return candidate


def worthwhile_payload(content_type: str, body: bytes, url: str) -> tuple[bool, dict[str, Any]]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    info: dict[str, Any] = {"contentType": normalized, "bytes": len(body)}
    if body.startswith(b"%PDF") or normalized == "application/pdf":
        info["kind"] = "pdf"
        return True, info
    if normalized in {
        "application/zip",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }:
        info["kind"] = "document"
        return len(body) >= 1024, info
    if normalized.startswith("image/") or re.search(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", url, re.I):
        if len(body) < 12_000:
            return False, info
        try:
            with Image.open(io.BytesIO(body)) as image:
                width, height = image.size
                info.update(kind="image", width=width, height=height, format=image.format)
                return width >= 500 and height >= 300, info
        except Exception as exc:
            info["imageError"] = repr(exc)
            return False, info
    return False, info


class AssetStore:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.assets_dir = output / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.hashes: dict[str, str] = {}
        self.records: list[dict[str, Any]] = []

    def add(
        self,
        *,
        url: str,
        body: bytes,
        content_type: str,
        content_disposition: str = "",
        source: str,
        status: int | None = None,
    ) -> dict[str, Any] | None:
        keep, info = worthwhile_payload(content_type, body, url)
        if not keep:
            return None
        digest = hashlib.sha256(body).hexdigest()
        if digest in self.hashes:
            record = {
                "url": url,
                "source": source,
                "duplicateOf": self.hashes[digest],
                "sha256": digest,
                **info,
            }
            self.records.append(record)
            return record
        extension = choose_extension(content_type, url, content_disposition)
        filename = content_filename(url, content_disposition, len(self.hashes) + 1, extension)
        destination = self.assets_dir / filename
        if destination.exists():
            destination = self.assets_dir / f"{destination.stem}_{len(self.hashes)+1:04d}{destination.suffix}"
        destination.write_bytes(body)
        relative = str(destination.relative_to(self.output))
        self.hashes[digest] = relative
        record = {
            "url": url,
            "source": source,
            "file": relative,
            "status": status,
            "sha256": digest,
            **info,
        }
        self.records.append(record)
        return record


async def capture_page(
    context: BrowserContext,
    url: str,
    label: str,
    output: Path,
    candidates: set[str],
    network_records: list[dict[str, Any]],
    response_tasks: list[asyncio.Task[Any]],
    store: AssetStore,
) -> None:
    page = await context.new_page()

    async def consume_response(response: Response) -> None:
        target = decode_repeated(response.url)
        headers = await response.all_headers()
        content_type = headers.get("content-type", "")
        record: dict[str, Any] = {
            "url": target,
            "status": response.status,
            "contentType": content_type,
            "contentLength": headers.get("content-length"),
            "resourceType": response.request.resource_type,
        }
        network_records.append(record)
        if any(marker.lower() in target.lower() for marker in INTERESTING_MARKERS):
            candidates.add(target)
        if response.status >= 400:
            return
        should_read = (
            content_type.lower().startswith("image/")
            or "application/pdf" in content_type.lower()
            or "attachment" in headers.get("content-disposition", "").lower()
        ) and any(marker.lower() in target.lower() for marker in INTERESTING_MARKERS)
        if not should_read:
            return
        try:
            body = await response.body()
            if len(body) <= 250 * 1024 * 1024:
                saved = store.add(
                    url=target,
                    body=body,
                    content_type=content_type,
                    content_disposition=headers.get("content-disposition", ""),
                    source=f"browser:{label}",
                    status=response.status,
                )
                if saved:
                    record["saved"] = saved.get("file") or saved.get("duplicateOf")
        except Exception as exc:
            record["bodyError"] = repr(exc)

    def on_response(response: Response) -> None:
        response_tasks.append(asyncio.create_task(consume_response(response)))

    page.on("response", on_response)
    navigation_error = None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(12000)
        for _ in range(8):
            await page.mouse.wheel(0, 2400)
            await page.wait_for_timeout(800)
    except Exception as exc:
        navigation_error = repr(exc)

    try:
        html_text = await page.content()
        (output / "pages" / f"{label}.html").parent.mkdir(parents=True, exist_ok=True)
        (output / "pages" / f"{label}.html").write_text(html_text, encoding="utf-8")
        decoded = decode_repeated(html_text)
        for match in re.findall(r"https?(?::|%3A)(?:\\?/\\?/|%2F%2F)[^\"'<>\s]+", decoded, re.I):
            candidate = decode_repeated(match).strip(")\"'.,;")
            if candidate.startswith(("http://", "https://")):
                candidates.add(candidate)
    except Exception as exc:
        (output / "pages" / f"{label}_html_error.txt").write_text(repr(exc), encoding="utf-8")

    try:
        links = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('*')).flatMap((el) => {
              const keys = ['href', 'src', 'data-src', 'data-link', 'data-url', 'data-attachment-url', 'data-original'];
              return keys.map((key) => el.getAttribute?.(key)).filter(Boolean);
            })
            """
        )
        for value in links:
            candidate = decode_repeated(urljoin(page.url, str(value)))
            if candidate.startswith(("http://", "https://")):
                candidates.add(candidate)
    except Exception:
        pass

    try:
        text = await page.locator("body").inner_text(timeout=5000)
        (output / "pages" / f"{label}.txt").write_text(text, encoding="utf-8")
    except Exception as exc:
        (output / "pages" / f"{label}_text_error.txt").write_text(repr(exc), encoding="utf-8")
    try:
        await page.screenshot(path=str(output / "pages" / f"{label}.png"), full_page=True)
    except Exception as exc:
        (output / "pages" / f"{label}_screenshot_error.txt").write_text(repr(exc), encoding="utf-8")
    dump_json(
        output / "pages" / f"{label}_meta.json",
        {"requestedUrl": url, "finalUrl": page.url, "title": await page.title(), "navigationError": navigation_error},
    )
    await page.close()


async def main_async(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    store = AssetStore(output)
    candidates: set[str] = set()
    network_records: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task[Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=ko-KR",
                "--window-size=1365,900",
            ],
        )
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1365, "height": 900},
            service_workers="allow",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        urls = [template.format(blog_id=args.blog_id, log_no=args.log_no) for template in PAGE_URLS]
        for index, url in enumerate(urls, 1):
            await capture_page(
                context,
                url,
                f"page_{index}",
                output,
                candidates,
                network_records,
                response_tasks,
                store,
            )
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        cookies = await context.cookies()
        dump_json(output / "browser_cookies.json", cookies)
        await browser.close()

    for record in network_records:
        url = decode_repeated(str(record.get("url") or ""))
        if url.startswith(("http://", "https://")):
            candidates.add(url)
    dump_json(output / "network.json", network_records)
    (output / "candidate_urls.txt").write_text("\n".join(sorted(candidates)), encoding="utf-8")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
            "Referer": f"https://blog.naver.com/{args.blog_id}/{args.log_no}",
        }
    )
    for cookie in cookies:
        try:
            session.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/")
            )
        except Exception:
            continue

    http_records = []
    selected_candidates = [
        url for url in sorted(candidates)
        if any(marker.lower() in url.lower() for marker in INTERESTING_MARKERS)
    ]
    for index, url in enumerate(selected_candidates, 1):
        record: dict[str, Any] = {"url": url}
        try:
            response = session.get(url, timeout=(15, 60), allow_redirects=True, stream=True)
            record.update(status=response.status_code, finalUrl=response.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            disposition = response.headers.get("content-disposition", "")
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 250 * 1024 * 1024:
                    raise RuntimeError("payload exceeds 250 MiB")
                chunks.append(chunk)
            body = b"".join(chunks)
            record.update(bytes=len(body), contentType=content_type)
            saved = store.add(
                url=response.url,
                body=body,
                content_type=content_type,
                content_disposition=disposition,
                source="http-session",
                status=response.status_code,
            )
            if saved:
                record["saved"] = saved.get("file") or saved.get("duplicateOf")
        except Exception as exc:
            record["error"] = repr(exc)
        http_records.append(record)

    dump_json(output / "http_fetch.json", http_records)
    dump_json(output / "asset_manifest.json", store.records)
    summary = {
        "blogId": args.blog_id,
        "logNo": args.log_no,
        "candidateUrlCount": len(candidates),
        "networkResponseCount": len(network_records),
        "assetRecordCount": len(store.records),
        "uniqueAssetCount": len(store.hashes),
        "pdfCount": sum(1 for record in store.records if record.get("kind") == "pdf" and record.get("file")),
        "documentCount": sum(1 for record in store.records if record.get("kind") == "document" and record.get("file")),
        "imageCount": sum(1 for record in store.records if record.get("kind") == "image" and record.get("file")),
    }
    dump_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blog-id", required=True)
    parser.add_argument("--log-no", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
