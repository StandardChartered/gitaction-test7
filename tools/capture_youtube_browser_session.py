#!/usr/bin/env python3
"""Create a same-IP guest YouTube browser session and capture player evidence.

The script uses a real Chromium instance, exports its cookies in Netscape format,
records YouTube player API traffic, saves player responses/configuration, and
captures diagnostics for both watch and embedded player pages.  No account is
used; this is an anonymous guest session created on the same runner that later
invokes yt-dlp.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Response, async_playwright


INTERESTING_URL_PARTS = (
    "/youtubei/v1/player",
    "/youtubei/v1/next",
    "/api/timedtext",
    "storyboard",
    "googlevideo.com",
    "videoplayback",
)


def safe_name(value: str, limit: int = 150) -> str:
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    return value[:limit].strip("_") or "item"


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_netscape_cookies(path: Path, cookies: list[dict[str, Any]]) -> None:
    lines = ["# Netscape HTTP Cookie File", "# Generated from an anonymous Playwright guest session", ""]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        cookie_path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = int(float(cookie.get("expires") or 0))
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        lines.append("\t".join((domain, include_subdomains, cookie_path, secure, str(expires), name, value)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def dismiss_consent(page: Page) -> None:
    candidates = [
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('동의')",
        "button:has-text('모두 동의')",
        "button:has-text('모두 수락')",
        "form[action*='consent'] button",
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1200):
                await locator.click(timeout=3000)
                await page.wait_for_timeout(2000)
                return
        except Exception:
            continue


async def extract_page_state(page: Page) -> dict[str, Any]:
    try:
        return await page.evaluate(
            """
            () => {
              const cfgGet = (key) => {
                try { return window.ytcfg?.get?.(key) ?? window.ytcfg?.data_?.[key] ?? null; }
                catch (_) { return null; }
              };
              let playerResponse = window.ytInitialPlayerResponse ?? null;
              if (!playerResponse) {
                try {
                  const raw = window.ytplayer?.config?.args?.player_response;
                  if (typeof raw === 'string') playerResponse = JSON.parse(raw);
                  else if (raw) playerResponse = raw;
                } catch (_) {}
              }
              const video = document.querySelector('video');
              return {
                href: location.href,
                title: document.title,
                bodyText: (document.body?.innerText || '').slice(0, 30000),
                visitorData: cfgGet('VISITOR_DATA'),
                apiKey: cfgGet('INNERTUBE_API_KEY'),
                clientName: cfgGet('INNERTUBE_CLIENT_NAME'),
                clientVersion: cfgGet('INNERTUBE_CLIENT_VERSION'),
                delegatedSessionId: cfgGet('DELEGATED_SESSION_ID'),
                serializedDelegationContext: cfgGet('SERIALIZED_DELEGATION_CONTEXT'),
                idToken: cfgGet('ID_TOKEN'),
                rolloutToken: cfgGet('ROLLOUT_TOKEN'),
                experimentsToken: cfgGet('EXPERIMENTS_TOKEN'),
                playerResponse,
                initialData: window.ytInitialData ?? null,
                videoState: video ? {
                  currentTime: video.currentTime,
                  duration: video.duration,
                  paused: video.paused,
                  readyState: video.readyState,
                  networkState: video.networkState,
                  currentSrc: video.currentSrc,
                  error: video.error ? {code: video.error.code, message: video.error.message} : null,
                } : null,
                webdriver: navigator.webdriver,
                userAgent: navigator.userAgent,
              };
            }
            """
        )
    except Exception as exc:
        return {"href": page.url, "error": repr(exc)}


async def browser_player_request(page: Page, video_id: str) -> dict[str, Any]:
    try:
        return await page.evaluate(
            """
            async ({videoId}) => {
              const cfgGet = (key) => {
                try { return window.ytcfg?.get?.(key) ?? window.ytcfg?.data_?.[key] ?? null; }
                catch (_) { return null; }
              };
              const key = cfgGet('INNERTUBE_API_KEY');
              const clientName = cfgGet('INNERTUBE_CLIENT_NAME') || 'WEB';
              const clientVersion = cfgGet('INNERTUBE_CLIENT_VERSION');
              const visitorData = cfgGet('VISITOR_DATA');
              if (!key || !clientVersion) {
                return {ok: false, reason: 'missing innertube configuration', key: !!key, clientVersion};
              }
              const payload = {
                context: {
                  client: {
                    clientName,
                    clientVersion,
                    hl: 'ko',
                    gl: 'KR',
                    visitorData,
                    utcOffsetMinutes: 540,
                  },
                  request: {useSsl: true},
                  user: {lockedSafetyMode: false},
                },
                videoId,
                playbackContext: {
                  contentPlaybackContext: {
                    html5Preference: 'HTML5_PREF_WANTS',
                    lactMilliseconds: '-1',
                  },
                },
                contentCheckOk: true,
                racyCheckOk: true,
              };
              const response = await fetch(`/youtubei/v1/player?key=${encodeURIComponent(key)}&prettyPrint=false`, {
                method: 'POST',
                credentials: 'include',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify(payload),
              });
              let data = null;
              try { data = await response.json(); }
              catch (_) { data = {raw: await response.text()}; }
              return {ok: response.ok, status: response.status, payload, data};
            }
            """,
            {"videoId": video_id},
        )
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


async def capture_page(
    context: BrowserContext,
    url: str,
    label: str,
    video_id: str,
    output: Path,
    response_tasks: list[asyncio.Task[Any]],
    traffic: list[dict[str, Any]],
) -> dict[str, Any]:
    page = await context.new_page()

    async def consume_response(response: Response) -> None:
        request = response.request
        target = response.url
        if not any(part in target for part in INTERESTING_URL_PARTS):
            return
        entry: dict[str, Any] = {
            "url": target,
            "status": response.status,
            "method": request.method,
            "resourceType": request.resource_type,
            "requestHeaders": await request.all_headers(),
            "responseHeaders": await response.all_headers(),
            "postData": request.post_data,
        }
        parsed = urlparse(target)
        base = safe_name(f"{label}_{len(traffic)+1:04d}_{parsed.netloc}_{Path(parsed.path).name}")
        try:
            content_type = (await response.all_headers()).get("content-type", "")
            if "json" in content_type or "/youtubei/" in target:
                body = await response.body()
                if len(body) <= 25 * 1024 * 1024:
                    body_path = output / "traffic" / f"{base}.json"
                    body_path.parent.mkdir(parents=True, exist_ok=True)
                    body_path.write_bytes(body)
                    entry["bodyFile"] = str(body_path.relative_to(output))
            elif "timedtext" in target and response.status < 400:
                body = await response.body()
                if len(body) <= 50 * 1024 * 1024:
                    suffix = ".xml"
                    body_path = output / "traffic" / f"{base}{suffix}"
                    body_path.parent.mkdir(parents=True, exist_ok=True)
                    body_path.write_bytes(body)
                    entry["bodyFile"] = str(body_path.relative_to(output))
        except Exception as exc:
            entry["bodyError"] = repr(exc)
        traffic.append(entry)

    def on_response(response: Response) -> None:
        response_tasks.append(asyncio.create_task(consume_response(response)))

    page.on("response", on_response)
    navigation_error = None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        navigation_error = repr(exc)
    await dismiss_consent(page)
    await page.wait_for_timeout(10000)

    for selector in (".ytp-large-play-button", "button.ytp-play-button", "#movie_player"):
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1000):
                await locator.click(timeout=3000, force=True)
                break
        except Exception:
            continue
    await page.wait_for_timeout(12000)

    state = await extract_page_state(page)
    state["navigationError"] = navigation_error
    dump_json(output / f"{label}_state.json", state)
    try:
        (output / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    except Exception as exc:
        (output / f"{label}_html_error.txt").write_text(repr(exc), encoding="utf-8")
    try:
        await page.screenshot(path=str(output / f"{label}.png"), full_page=True)
    except Exception as exc:
        (output / f"{label}_screenshot_error.txt").write_text(repr(exc), encoding="utf-8")

    api_result = await browser_player_request(page, video_id)
    dump_json(output / f"{label}_browser_player_request.json", api_result)
    await page.wait_for_timeout(3000)
    await page.close()
    return state


async def main_async(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    traffic: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task[Any]] = []
    started = time.time()

    async with async_playwright() as playwright:
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--lang=ko-KR",
            "--window-size=1365,900",
            "--autoplay-policy=no-user-gesture-required",
        ]
        browser = await playwright.chromium.launch(headless=False, args=launch_args)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1365, "height": 900},
            screen={"width": 1365, "height": 900},
            java_script_enabled=True,
            service_workers="allow",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        states = []
        states.append(
            await capture_page(
                context,
                f"https://www.youtube.com/watch?v={args.video_id}&hl=ko&gl=KR",
                "watch",
                args.video_id,
                output,
                response_tasks,
                traffic,
            )
        )
        states.append(
            await capture_page(
                context,
                f"https://www.youtube.com/embed/{args.video_id}?hl=ko&gl=KR&playsinline=1&autoplay=0",
                "embed",
                args.video_id,
                output,
                response_tasks,
                traffic,
            )
        )

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

        cookies = await context.cookies()
        dump_json(output / "cookies.json", cookies)
        write_netscape_cookies(output / "youtube_cookies.txt", cookies)
        await context.storage_state(path=str(output / "storage_state.json"))
        await browser.close()

    dump_json(output / "traffic.json", traffic)
    visitor_data = next((state.get("visitorData") for state in states if state.get("visitorData")), None)
    player_responses = []
    for state in states:
        response = state.get("playerResponse")
        if isinstance(response, dict):
            player_responses.append(response)
    summary = {
        "videoId": args.video_id,
        "visitorData": visitor_data,
        "cookieNames": sorted({str(cookie.get('name')) for cookie in cookies}),
        "playerResponseCount": len(player_responses),
        "trafficCount": len(traffic),
        "elapsedSeconds": round(time.time() - started, 3),
        "pages": [
            {
                "href": state.get("href"),
                "title": state.get("title"),
                "playabilityStatus": (state.get("playerResponse") or {}).get("playabilityStatus")
                if isinstance(state.get("playerResponse"), dict)
                else None,
                "videoState": state.get("videoState"),
            }
            for state in states
        ],
    }
    dump_json(output / "session_summary.json", summary)
    if visitor_data:
        (output / "visitor_data.txt").write_text(str(visitor_data), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
