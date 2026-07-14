"""Description HTML cleaning and image URL validation.

The previous pipeline shipped raw scraped eBay HTML straight into the feed
(only whitespace-collapsed and length-truncated) and never checked image URLs
at all, despite both being documented requirements. This module actually
implements them.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import bleach
import requests

logger = logging.getLogger("onbuy_sync")

ALLOWED_TAGS = ["p", "ul", "ol", "li", "b", "strong", "i", "em", "br"]

# Heuristic patterns for eBay/seller boilerplate that survive tag-stripping
# because they're plain text, not markup. Not exhaustive - review real output
# periodically and extend this list as new noise shows up.
_NOISE_PATTERNS = [
    r"check\s*out\s*(my|our)\s*(other\s*)?(items|listings|ebay\s*store)",
    r"visit\s*(my|our)\s*ebay\s*(shop|store)",
    r"add\s*(me|us)\s*to\s*your\s*(favou?rite\s*)?sellers?",
    r"\d{1,3}\s*%\s*positive\s*feedback",
    r"please\s*leave\s*(us\s*|me\s*)?(a\s*)?(positive\s*)?feedback",
    r"we\s*strive\s*for\s*5\s*star",
    r"ebay\.(co\.uk|com)\S*",
    r"paypal\.\S*",
    r"https?://\S+",  # any bare URL left after tag-stripping is an external link
    # Leftover template title from this seller's bulk-listing tool - appears
    # verbatim at the start of many otherwise-unrelated listings' descriptions
    # (confirmed 2026-07-04: same fixed phrase prefixed onto ~9 different,
    # correctly-distinct product descriptions).
    r"3D\s*Optical\s*Illusion\s*Endless\s*Abyss\s*Floor\s*Mat\s*",
]
# A single (?i) at the start applies to every alternative - Python 3.11+
# rejects a repeated inline flag inside each alternative of a joined pattern.
_NOISE_RE = re.compile("(?i)" + "|".join(_NOISE_PATTERNS))

# bleach's strip=True unwraps disallowed tags but keeps their *text* content -
# fine for a stray <div>/<span>, but for <script>/<style> that would leak raw
# JS/CSS straight into the "sanitized" description. Delete these tags and
# everything inside them before bleach ever sees the markup.
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def sanitize_description(html, limit=45000):
    if not html:
        return ""

    html = str(html)
    html = _SCRIPT_STYLE_RE.sub("", html)

    # Keep only a small safe-formatting allowlist. strip=True drops disallowed
    # tags (div/span/font/a/img/script/style/...) but keeps their text content,
    # so paragraphs and lists survive while inline styling, scripts and links
    # to eBay/social/seller pages don't.
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes={}, strip=True)

    # Remove seller-boilerplate sentences the tag-level cleaning can't catch
    # since they're plain text, not markup.
    cleaned = _NOISE_RE.sub("", cleaned)

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) > limit:
        cleaned = cleaned[:limit]

    return cleaned


_IMAGE_CHECK_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OnBuySyncBot/1.0; +https://onbuy.com)"}


def _check_image(url, timeout=4.0):
    """Return url if it resolves to a reachable HTTPS image, else None."""
    if not url or not url.lower().startswith("https://"):
        return None
    try:
        # Several image CDNs (observed on both Wikimedia and eBay's own) return
        # 403/405 for requests with no User-Agent, treating them as bots - always send one.
        resp = requests.head(url, timeout=timeout, allow_redirects=True, headers=_IMAGE_CHECK_HEADERS)
        if resp.status_code in (403, 405):  # some CDNs reject HEAD outright
            resp = requests.get(url, timeout=timeout, stream=True, headers={**_IMAGE_CHECK_HEADERS, "Range": "bytes=0-0"})
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code < 400 and content_type.startswith("image/"):
            return url
        logger.info("Rejected image (status=%s, content-type=%s): %s", resp.status_code, content_type, url)
    except requests.exceptions.RequestException as exc:
        logger.info("Rejected image (unreachable: %s): %s", exc, url)
    return None


def validate_images(urls, max_images=10, max_workers=8):
    """Validate a list of image URLs concurrently, preserving input order,
    keeping only HTTPS URLs that resolve to a real image, capped at max_images.
    """
    candidates = [u.strip() for u in urls if u and u.strip()][: max_images * 2]
    if not candidates:
        return []

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_image, url): url for url in candidates}
        for future in as_completed(futures):
            url = futures[future]
            results[url] = future.result()

    valid = [u for u in candidates if results.get(u)]
    return valid[:max_images]
