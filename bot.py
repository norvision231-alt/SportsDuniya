"""Fetch sports RSS items and publish structured updates to Telegram."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("sports_news_bot")

HASHES_PATH = Path(__file__).with_name("posted_hashes.json")
DEFAULT_FEEDS = "https://feeds.bbci.co.uk/sport/rss.xml"
TELEGRAM_API = "https://api.telegram.org"
MAX_TELEGRAM_LENGTH = 4096


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required GitHub Actions secret/environment variable {name} is missing")
    return value


def plain_text(value: Any) -> str:
    """Convert feed HTML/entities to readable, whitespace-normalized text."""
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def load_posted_hashes() -> set[str]:
    try:
        data = json.loads(HASHES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Could not read %s (%s); starting with no stored hashes", HASHES_PATH, exc)
        return set()

    if isinstance(data, list):
        return {str(item) for item in data if item}
    if isinstance(data, dict):
        # Accept the object format too, so older/custom state files remain usable.
        values = data.get("hashes", [])
        if isinstance(values, list):
            return {str(item) for item in values if item}
    LOGGER.warning("Unexpected posted_hashes.json format; starting with no stored hashes")
    return set()


def save_posted_hashes(hashes: set[str]) -> None:
    temporary_path = HASHES_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(sorted(hashes), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(HASHES_PATH)


def article_hash(entry: Any) -> str:
    identity = plain_text(entry.get("link")) or plain_text(entry.get("id")) or plain_text(entry.get("title"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def published_text(entry: Any) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        except (TypeError, ValueError):
            pass
    return "Date not provided"


def category_text(entry: Any) -> str:
    tags = entry.get("tags", [])
    for tag in tags:
        term = plain_text(tag.get("term")) if isinstance(tag, dict) else ""
        if term:
            return term
    return "Sports"


def feed_items(feed_urls: list[str]) -> list[tuple[Any, str]]:
    items: list[tuple[Any, str]] = []
    for feed_url in feed_urls:
        LOGGER.info("Reading feed %s", feed_url)
        parsed = feedparser.parse(feed_url)
        if parsed.bozo and not parsed.entries:
            LOGGER.warning("Could not parse feed %s: %s", feed_url, parsed.get("bozo_exception"))
            continue
        source = plain_text(parsed.feed.get("title")) or urlparse(feed_url).hostname or "Sports news"
        items.extend((entry, source) for entry in parsed.entries)
    return items


def format_message(entry: Any, source: str) -> str:
    title = plain_text(entry.get("title")) or "Untitled sports news"
    summary = plain_text(entry.get("summary"))
    if not summary and entry.get("content"):
        summary = plain_text(entry["content"][0].get("value", ""))
    if not summary:
        summary = "Open the article for the full story."
    link = plain_text(entry.get("link"))
    if not link or urlparse(link).scheme not in {"http", "https"}:
        link = ""

    # Keep messages comfortably below Telegram's 4096-character limit.
    summary_budget = 900
    if len(summary) > summary_budget:
        summary = summary[: summary_budget - 1].rsplit(" ", 1)[0] + "…"

    escaped_title = html.escape(title)
    escaped_summary = html.escape(summary)
    escaped_category = html.escape(category_text(entry))
    escaped_source = html.escape(source)
    escaped_date = html.escape(published_text(entry))
    article_link = f'<a href="{html.escape(link, quote=True)}">Read full story</a>' if link else ""

    message = (
        "🏆 <b>SPORTS NEWS</b>\n\n"
        f"<b>Headline:</b> {escaped_title}\n\n"
        f"<b>Summary:</b> {escaped_summary}\n\n"
        f"<b>Category:</b> {escaped_category}\n"
        f"<b>Published:</b> {escaped_date}\n"
        f"<b>Source:</b> {escaped_source}"
    )
    if article_link:
        message += f"\n{article_link}"
    if len(message) > MAX_TELEGRAM_LENGTH:
        message = message[: MAX_TELEGRAM_LENGTH - 1].rsplit("\n", 1)[0] + "…"
    return message


def send_telegram_message(token: str, channel_id: str, text: str) -> None:
    response = requests.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={
            "chat_id": channel_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result.get('description', 'unknown error')}")


def main() -> int:
    token = required_env("TELEGRAM_BOT_TOKEN")
    channel_id = required_env("TELEGRAM_CHANNEL_ID")
    feed_urls = [
        url.strip()
        for url in os.getenv("NEWS_FEED_URLS", DEFAULT_FEEDS).split(",")
        if url.strip()
    ]
    try:
        max_articles = max(1, int(os.getenv("MAX_ARTICLES_PER_RUN", "5")))
    except ValueError:
        LOGGER.warning("Invalid MAX_ARTICLES_PER_RUN; using 5")
        max_articles = 5

    posted = load_posted_hashes()
    candidates: list[tuple[Any, str, str]] = []
    for entry, source in feed_items(feed_urls):
        digest = article_hash(entry)
        if digest not in posted and plain_text(entry.get("title")):
            candidates.append((entry, source, digest))

    # Feeds generally list newest first. Reverse the selected batch so posts arrive
    # in chronological order when several hourly items are waiting.
    candidates = candidates[:max_articles]
    candidates.reverse()
    if not candidates:
        LOGGER.info("No new sports articles to publish")
        return 0

    sent = 0
    for entry, source, digest in candidates:
        try:
            send_telegram_message(token, channel_id, format_message(entry, source))
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            LOGGER.error("Failed to send '%s': %s", plain_text(entry.get("title")), exc)
            # Keep the error visible to Actions and do not mark unsent items as posted.
            return 1
        posted.add(digest)
        save_posted_hashes(posted)
        sent += 1
        LOGGER.info("Published: %s", plain_text(entry.get("title")))

    LOGGER.info("Published %d new article(s)", sent)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOGGER.exception("Sports news bot failed")
        sys.exit(1)
