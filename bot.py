"""Fetch free sports RSS feeds and publish structured updates to Telegram."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
HTTP_TIMEOUT = (10, 25)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required GitHub secret {name} is missing")
    return value


def plain_text(value: Any) -> str:
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
        # Do not silently overwrite corrupted state: that can repost old stories.
        raise RuntimeError(f"Cannot read {HASHES_PATH}: {exc}") from exc
    if isinstance(data, list):
        return {str(item) for item in data if item}
    if isinstance(data, dict) and isinstance(data.get("hashes"), list):
        return {str(item) for item in data["hashes"] if item}
    raise RuntimeError(f"Unexpected format in {HASHES_PATH}; expected a JSON list of hashes")


def save_posted_hashes(hashes: set[str]) -> None:
    # Keep the repository state small while retaining ample duplicate history.
    retained = sorted(hashes)[-5000:]
    temporary_path = HASHES_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(retained, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(HASHES_PATH)


def article_hash(entry: Any) -> str:
    identity = plain_text(entry.get("link")) or plain_text(entry.get("id")) or plain_text(entry.get("title"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
    return None


def published_text(entry: Any) -> str:
    published = entry_datetime(entry)
    return published.strftime("%d %b %Y, %H:%M UTC") if published else "Date not provided"


def category_text(entry: Any) -> str:
    for tag in entry.get("tags", []):
        term = plain_text(tag.get("term")) if isinstance(tag, dict) else ""
        if term:
            return term
    return "Sports"


def get_feed(feed_url: str, session: requests.Session) -> list[tuple[Any, str]]:
    for attempt in range(3):
        try:
            response = session.get(feed_url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "SportsDuniyaNewsBot/1.0"})
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"Invalid/empty RSS feed: {parsed.get('bozo_exception')}")
            source = plain_text(parsed.feed.get("title")) or urlparse(feed_url).hostname or "Sports news"
            return [(entry, source) for entry in parsed.entries]
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                LOGGER.warning("Skipping feed %s after retries: %s", feed_url, exc)
                return []
            time.sleep(2**attempt)
    return []


def format_message(entry: Any, source: str) -> str:
    title = plain_text(entry.get("title")) or "Sports update"
    summary = plain_text(entry.get("summary"))
    if not summary and entry.get("content"):
        summary = plain_text(entry["content"][0].get("value", ""))
    if not summary:
        summary = "Read the full story for details."
    if len(summary) > 800:
        summary = summary[:799].rsplit(" ", 1)[0] + "…"

    link = plain_text(entry.get("link"))
    if urlparse(link).scheme not in {"http", "https"}:
        link = ""

    message = (
        "🏆 <b>SPORTS UPDATE</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"{html.escape(summary)}\n\n"
        f"🏷 {html.escape(category_text(entry))}  ·  "
        f"🕒 {html.escape(published_text(entry))}\n"
        f"📰 {html.escape(source)}"
    )
    if link:
        message += f'\n\n<a href="{html.escape(link, quote=True)}">Read the full story</a>'
    return message[:MAX_TELEGRAM_LENGTH]


def send_telegram_message(token: str, channel_id: str, text: str, session: requests.Session) -> None:
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(4):
        response = session.post(url, json=payload, timeout=HTTP_TIMEOUT)
        try:
            result = response.json()
        except ValueError:
            result = {}

        if response.ok and result.get("ok"):
            return
        if response.status_code == 429 and attempt < 3:
            retry_after = result.get("parameters", {}).get("retry_after", 2**attempt)
            time.sleep(min(max(int(retry_after), 1), 60))
            continue
        # Retry temporary server failures; do not retry ambiguous timeouts, which
        # could cause a duplicate if Telegram accepted the message but the reply was lost.
        if response.status_code >= 500 and attempt < 3:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()
        raise RuntimeError(f"Telegram rejected message: {result.get('description', 'unknown API error')}")
    raise RuntimeError("Telegram remained unavailable after retries")


def main() -> int:
    token = required_env("TELEGRAM_BOT_TOKEN")
    channel_id = required_env("TELEGRAM_CHANNEL_ID")
    feed_urls = [url.strip() for url in os.getenv("NEWS_FEED_URLS", DEFAULT_FEEDS).split(",") if url.strip()]
    if not feed_urls:
        raise RuntimeError("NEWS_FEED_URLS contains no valid feed URLs")

    try:
        max_articles = min(max(int(os.getenv("MAX_ARTICLES_PER_RUN", "3")), 1), 10)
        max_age_hours = max(int(os.getenv("MAX_ARTICLE_AGE_HOURS", "36")), 1)
    except ValueError as exc:
        raise RuntimeError("MAX_ARTICLES_PER_RUN and MAX_ARTICLE_AGE_HOURS must be integers") from exc

    posted = load_posted_hashes()
    session = requests.Session()
    candidates: dict[str, tuple[Any, str, datetime | None]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for feed_url in feed_urls:
        for entry, source in get_feed(feed_url, session):
            digest = article_hash(entry)
            title = plain_text(entry.get("title"))
            published = entry_datetime(entry)
            if digest in posted or not title:
                continue
            if published and published < cutoff:
                continue
            # De-duplicate the same article appearing in more than one RSS source.
            candidates.setdefault(digest, (entry, source, published))

    ordered = sorted(
        candidates.items(),
        key=lambda item: item[1][2] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:max_articles]
    if not ordered:
        LOGGER.info("No new recent sports stories to publish")
        return 0

    sent = 0
    for digest, (entry, source, _published) in reversed(ordered):
        try:
            send_telegram_message(token, channel_id, format_message(entry, source), session)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            LOGGER.error("Could not publish '%s': %s", plain_text(entry.get("title")), exc)
            return 1
        posted.add(digest)
        save_posted_hashes(posted)
        sent += 1
        LOGGER.info("Published: %s", plain_text(entry.get("title")))

    LOGGER.info("Published %d story/stories", sent)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOGGER.exception("Sports news bot failed")
        sys.exit(1)
