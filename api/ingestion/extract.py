import logging
import os
import re
import xml.etree.ElementTree as ET

import trafilatura

logger = logging.getLogger(__name__)

RESOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "resources")


def slugify_url(url: str) -> str:
    """Convert URL to a filesystem-safe slug for local storage."""
    slug = re.sub(r"https?://", "", url)
    slug = re.sub(r"[^a-zA-Z0-9]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:120]


def extract_content(url: str) -> dict | None:
    """Fetch and extract content from a URL. Returns dict with content and metadata, or None on failure.

    Tries Trafilatura first. Falls back to Playwright for JS-rendered pages.
    """
    result = _extract_with_trafilatura(url)
    if result:
        return result

    result = _extract_with_playwright(url)
    if result:
        return result

    logger.warning("Extraction failed for %s: both Trafilatura and Playwright returned empty", url)
    return None


def _extract_with_trafilatura(url: str) -> dict | None:
    """Attempt extraction via Trafilatura (HTTP fetch + boilerplate removal)."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return _parse_with_trafilatura(downloaded, url)


def _extract_with_playwright(url: str) -> dict | None:
    """Render page with Playwright, then extract with Trafilatura."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed, skipping fallback for %s", url)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            browser.close()
    except Exception as e:
        logger.warning("Playwright failed for %s: %s", url, e)
        return None

    return _parse_with_trafilatura(html, url)


def _parse_with_trafilatura(html: str, url: str) -> dict | None:
    """Parse HTML with Trafilatura and return extracted content with metadata."""
    content = trafilatura.extract(html, output_format="txt", include_links=False)
    if not content or len(content.strip()) < 100:
        return None

    title = ""
    xml_output = trafilatura.extract(html, output_format="xml", include_links=False)
    if xml_output:
        try:
            root = ET.fromstring(xml_output)
            title = root.attrib.get("title", "")
        except ET.ParseError:
            pass

    return {"content": content, "title": title, "url": url}


def save_extracted_content(url: str, content: str) -> str:
    """Save extracted text to local file. Returns the file path."""
    os.makedirs(RESOURCES_DIR, exist_ok=True)
    slug = slugify_url(url)
    path = os.path.join(RESOURCES_DIR, f"{slug}.md")
    with open(path, "w") as f:
        f.write(content)
    return path
