#!/usr/bin/env python3
"""Capture README previews of the reporting pages from contract fixtures.

The browser talks to a mocked same-origin API that serves
apps/web/tests/fixtures/analytics/*.json, so the images are previews of the UI only;
they do not show integrated server data. Build the web app first with
VITE_ANALYTICS_PAGES=enabled VITE_SCREEN_TRANSPORT=polling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "apps/web/tests/fixtures/analytics"
ORIGIN = "http://preview.invalid"
ROUTES = {
    "/api/v1/sources": "sources",
    "/api/v1/dashboard": "dashboard",
    "/api/v1/trends": "trend",
    "/api/v1/reports/execution-timeline": "timeline",
    "/api/v1/reports/executions": "executions",
    "/api/v1/reports/daily": "daily",
    "/api/v1/spc/xbar-r": "spc",
}


async def capture(chromium: str, web_dir: Path, out: Path) -> None:
    from playwright.async_api import async_playwright

    session = {
        "login": "demo-operator",
        "roles": ["operator", "production_manager"],
        "must_change_password": False,
        "csrf_token": "preview",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }

    async def handle(route):
        path = route.request.url[len(ORIGIN) :].split("?", 1)[0]
        for prefix in sorted(ROUTES, key=len, reverse=True):
            if path.startswith(prefix):
                body = (FIXTURES / f"{ROUTES[prefix]}.json").read_text(encoding="utf-8")
                return await route.fulfill(status=200, content_type="application/json", body=body)
        if path == "/auth/challenge":
            return await route.fulfill(
                status=200, content_type="application/json", body='{"csrf_token":"preview"}'
            )
        if path == "/auth/login":
            return await route.fulfill(
                status=200, content_type="application/json", body=json.dumps(session)
            )
        if path.startswith(("/api/", "/auth/", "/ws/")):
            return await route.fulfill(status=503, content_type="application/json", body="{}")
        file = web_dir / (path.lstrip("/") or "index.html")
        if not file.is_file():
            file = web_dir / "index.html"
        content_type = mimetypes.guess_type(str(file))[0] or "text/html"
        await route.fulfill(status=200, content_type=content_type, body=file.read_bytes())

    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul"
        )
        await page.route(f"{ORIGIN}/**", handle)
        await page.goto(ORIGIN + "/")
        await page.fill("#login", "demo-operator")
        await page.fill("#login-password", "preview")
        await page.click("#login-submit")
        await page.wait_for_selector("#current-login")
        for name in ("dashboard", "trends", "reports", "spc"):
            await page.click(f"#workspace-{name}")
            await page.wait_for_timeout(700)
            if name == "reports":
                await page.click(".data-table .link-button >> nth=0")
                await page.wait_for_timeout(400)
                await page.locator(".timeline-segment").first.scroll_into_view_if_needed()
            if name == "trends":
                chart = page.locator(".chart svg").first
                await chart.scroll_into_view_if_needed()
                box = await chart.bounding_box()
                await page.mouse.move(box["x"] + box["width"] * 0.72, box["y"] + 90)
            if name == "spc":
                await page.locator(".trend-warning").scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(out / f"preview-{name}.png"))
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default=shutil.which("chromium") or "chromium")
    parser.add_argument("--web-dir", type=Path, default=ROOT / "apps/web/dist")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/assets/screenshots")
    args = parser.parse_args()
    if not (args.web_dir / "index.html").is_file():
        print("FAIL: build apps/web with VITE_ANALYTICS_PAGES=enabled first")
        return 1
    asyncio.run(capture(args.chromium, args.web_dir.resolve(), args.out.resolve()))
    print("Saved fixture previews to " + str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
