"""Render self-contained export HTML with headless Chromium."""

from __future__ import annotations

from base64 import b64encode
from contextlib import contextmanager
from html import escape
from pathlib import Path

from playwright.sync_api import sync_playwright


_BROWSER_TIMEOUT_MS = 15_000
_IMAGE_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class HtmlDimensionsError(ValueError):
    """The laid-out page exceeds the requested screenshot bounds."""


def image_data_uri(path: Path) -> str:
    mime = _IMAGE_MIME_TYPES[path.suffix.lower()]
    return f"data:{mime};base64,{b64encode(path.read_bytes()).decode('ascii')}"


def html_document(title: str, css: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{css}</style>
</head>
<body>{body}</body>
</html>"""


@contextmanager
def _export_page(html: str, *, width: int, media: str):
    # Playwright's synchronous objects belong to the calling worker thread.
    with sync_playwright() as playwright:
        with playwright.chromium.launch(
            channel="chromium",
            args=["--no-sandbox", "--enable-logging=stderr", "--log-level=3"],
            timeout=_BROWSER_TIMEOUT_MS,
        ) as browser:
            page = browser.new_page(
                viewport={"width": width, "height": 960},
                device_scale_factor=1,
                offline=True,
                service_workers="block",
            )
            page.route("**/*", lambda route: route.abort())
            page.set_default_timeout(_BROWSER_TIMEOUT_MS)
            page.emulate_media(media=media)
            page.set_content(html, wait_until="load")
            page.wait_for_function("document.fonts.status === 'loaded'")
            if not page.evaluate("Array.from(document.images).every(image => image.naturalWidth > 0)"):
                raise ValueError("export illustration could not be decoded")
            yield page


def screenshot_html(html: str, *, width: int, max_height: int) -> bytes:
    with _export_page(html, width=width, media="screen") as page:
        dimensions = page.evaluate("""() => ({
            width: document.documentElement.scrollWidth,
            height: document.documentElement.scrollHeight,
        })""")
        if dimensions["width"] != width or dimensions["height"] > max_height:
            raise HtmlDimensionsError("export page exceeds screenshot dimensions")
        return page.screenshot(type="png", full_page=True, animations="disabled")


def print_pdf_html(html: str) -> bytes:
    with _export_page(html, width=560, media="print") as page:
        return page.pdf(format="A5", prefer_css_page_size=True, print_background=True)
