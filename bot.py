import os
import sys
import gc
import re
import time
import html
import json
import hashlib
import requests
import feedparser
from io import BytesIO
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RAW_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

BRAND_TAG = "@SportsDuniyaOfficial"
CHANNEL_LINK = "https://t.me/SportsDuniyaOfficial"

if "t.me/" in RAW_CHANNEL_ID:
    RAW_CHANNEL_ID = "@" + RAW_CHANNEL_ID.split("t.me/")[-1].strip("/")
elif RAW_CHANNEL_ID and not RAW_CHANNEL_ID.startswith("@") and not RAW_CHANNEL_ID.startswith("-"):
    RAW_CHANNEL_ID = "@" + RAW_CHANNEL_ID

TELEGRAM_CHANNEL_ID = RAW_CHANNEL_ID

RSS_FEEDS = [
    "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
    "https://feeds.feedburner.com/ndtvsports-latest",
    "https://feeds.feedburner.com/ndtvsports-cricket",
    "http://feeds.bbci.co.uk/sport/rss.xml",
    "https://www.espn.com/espn/rss/news",
    "https://www.skysports.com/rss/12040"
]

PRIORITY_KEYWORDS = [
    "india", "kohli", "rohit", "bumrah", "ipl", "bcci", "world cup", "champions trophy",
    "premier league", "champions league", "real madrid", "barcelona", "man utd", "liverpool",
    "arsenal", "messi", "ronaldo", "haaland", "mbappe", "ufc", "mma", "chess", "gukesh",
    "praggnanandhaa", "hockey", "neeraj chopra", "f1", "final", "record", "breaking", "win"
]

HASH_FILE = "posted_hashes.json"

def get_ist_now():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_tz)

def check_secrets():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        print(f"ERROR: Missing GitHub Secrets: {', '.join(missing)}")
        sys.exit(1)

def sync_git_pull():
    os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")

def sync_git_push():
    os.system('git config --global user.name "github-actions[bot]"')
    os.system('git config --global user.email "github-actions[bot]@users.noreply.github.com"')
    os.system("git add posted_hashes.json")
    os.system('git commit -m "Update bot state & settings" >/dev/null 2>&1 || true')
    os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")
    os.system("git push origin main >/dev/null 2>&1 || true")

def load_state():
    sync_git_pull()
    default_state = {
        "hashes": [],
        "titles": [],
        "last_post_ts": 0,
        "interval_mins": 60,
        "night_pause": True,
        "last_update_id": 0
    }
    if os.path.exists(HASH_FILE):
        try:
            with open(HASH_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    default_state["hashes"] = data
                    return default_state
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception:
            pass
    return default_state

def save_state(state):
    state["hashes"] = state["hashes"][-150:]
    state["titles"] = state["titles"][-100:]
    with open(HASH_FILE, "w") as f:
        json.dump(state, f)
    sync_git_push()

def is_duplicate(url_hash, title, state):
    if url_hash in state["hashes"]:
        return True
    clean_title = title.lower()
    for old_title in state["titles"]:
        if SequenceMatcher(None, clean_title, old_title).ratio() > 0.65:
            return True
    return False

def score_entry(entry, index):
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    score = max(0, 20 - index)
    for kw in PRIORITY_KEYWORDS:
        if kw in text:
            score += 10
    return score

def get_available_models():
    models = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        resp = requests.get(url, timeout=15)
        if resp.ok:
            discovered = []
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods and any(k in name for k in ["flash", "gemma"]):
                    if not any(bad in name for bad in ["2.5", "2.0", "1.5", "tts", "image", "audio", "vision"]):
                        discovered.append(name)
            discovered.sort(key=lambda x: (0 if "lite" in x else 1))
            if discovered:
                models = discovered[:4]
    except Exception:
        pass
    return models

def get_article_data(entry, url):
    image_url = None
    article_text = ""

    if "media_content" in entry and entry.media_content:
        image_url = entry.media_content[0].get("url")
    elif "media_thumbnail" in entry and entry.media_thumbnail:
        image_url = entry.media_thumbnail[0].get("url")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        if not image_url:
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                image_url = og_image["content"]
        
        paragraphs = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 40]
        article_text = " ".join(paragraphs[:6])
    except Exception:
        pass

    return image_url, article_text

def watermark_image_in_ram(image_bytes):
    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGBA")
        width, height = img.size

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(16, int(width * 0.028))
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, font_size)
                break
        if not font:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), BRAND_TAG, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        pad_x = int(font_size * 0.7)
        pad_y = int(font_size * 0.45)
        margin = int(width * 0.025)

        box_x2 = width - margin
        box_y2 = height - margin
        box_x1 = box_x2 - text_w - (pad_x * 2)
        box_y1 = box_y2 - text_h - (pad_y * 2)

        draw.rounded_rectangle(
            [box_x1, box_y1, box_x2, box_y2],
            radius=int(font_size * 0.5),
            fill=(15, 20, 30, 200),
            outline=(255, 195, 0, 230),
            width=max(1, int(font_size * 0.08))
        )
        draw.text((box_x1 + pad_x, box_y1 + pad_y - bbox[1]), BRAND_TAG, font=font, fill=(255, 255, 255, 255))

        combined = Image.alpha_composite(img, overlay).convert("RGB")
        out_buffer = BytesIO()
        combined.save(out_buffer, format="JPEG", quality=92)
        out_buffer.seek(0)
        out_buffer.name = "sports_duniya.jpg"
        return out_buffer

def fallback_inshorts_summary(title, description, extra_text):
    clean_desc = BeautifulSoup(description or "", "html.parser").get_text().strip()
    combined = f"{clean_desc} {extra_text}".strip()
    words = combined.split()
    body = " ".join(words[:58]) + ("..." if len(words) > 58 else "") if len(words) > 12 else title
    return (
        f"🔥 {title}\n\n"
        f"{body}\n\n"
        f"💬 What's your take on this? React below!\n\n"
        f"#SportsNews #Cricket #Football #SportsDuniya"
    )

def generate_inshorts_summary(title, description, extra_text, models):
    prompt = f"""
    You are the Chief Editor of '{BRAND_TAG}', a high-growth Inshorts-style Telegram sports channel.
    Rewrite this sports news into high-retention, SEO-optimized English following this EXACT 4-part structure:

    Line 1: [Sport Emoji like 🏏/⚽/🥊/♟️/🏎️] + A viral, high-hook headline (max 11 words, include main player/team name for search SEO, no asterisks or markdown).
    Blank line.
    Paragraph 1: Crisp Inshorts summary in strictly 50 to 60 words. Start with the biggest impact/score/fact in the first sentence. Include full searchable names of players, teams, and tournaments.
    Blank line.
    Line 2: A 1-line engaging fan hook starting with 💬 (e.g., "💬 Can India bounce back in the next match? React below!").
    Blank line.
    Line 3: 4 to 5 high-search hashtags (e.g., #Cricket #INDvsNZ #RohitSharma #Football #SportsDuniya).

    News Title: {title}
    News Summary: {description}
    Additional Context: {extra_text[:1200]}
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            resp = requests.post(url, json=payload, timeout=40)
            if resp.ok:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            time.sleep(3)
        except Exception:
            time.sleep(3)

    return fallback_inshorts_summary(title, description, extra_text)

def post_to_telegram(image_url, summary_text, source_link=""):
    # NDTV और अन्य साइट्स के 403 Forbidden को रोकने के लिए एडवांस्ड ब्राउज़र हेडर्स
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": source_link if source_link else "https://www.google.com/"
    }
    img_resp = requests.get(image_url, headers=headers, timeout=15)
    img_resp.raise_for_status()
    
    branded_buffer = watermark_image_in_ram(img_resp.content)
    
    lines = [line.strip() for line in summary_text.split("\n") if line.strip()]
    headline = html.escape(lines[0].replace("*", "").replace("#", ""))
    body = html.escape("\n\n".join(lines[1:]))
    
    caption = (
        f"<b>{headline}</b>\n\n"
        f"{body}\n\n"
        f"⚡ <b>{BRAND_TAG}</b>"
    )
    
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🔥 Join Sports Duniya", "url": CHANNEL_LINK}
            ]
        ]
    }
    
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": branded_buffer}
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup)
    }
    
    res = requests.post(tg_url, data=data, files=files, timeout=20)
    
    branded_buffer.close()
    del branded_buffer
    del img_resp
    gc.collect()
    
    if not res.ok:
        print(f"Telegram API Error ({res.status_code}): {res.text}")
        return False
    return True

def execute_news_post(state, models, force=False):
    ist_now = get_ist_now()
    interval_mins = int(state.get("interval_mins", 60))
    night_pause = state.get("night_pause", True)

    if not force:
        if night_pause and (0 <= ist_now.hour < 4):
            return False
        elapsed_mins = (int(time.time()) - state.get("last_post_ts", 0)) / 60
        if elapsed_mins < interval_mins:
            return False

    print(f"\n[POST TRIGGERED] IST: {ist_now.strftime('%I:%M %p')} | Interval: {interval_mins}m | Force: {force}")
    candidates = []
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for idx, entry in enumerate(feed.entries[:15]):
            link = entry.get("link", "")
            title = entry.get("title", "")
            if not link or not title:
                continue
            url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
            if is_duplicate(url_hash, title, state):
                continue
            score = score_entry(entry, idx)
            candidates.append((score, entry, link, url_hash, title))
            
    candidates.sort(key=lambda x: x[0], reverse=True)
    print(f"[SCAN] Found {len(candidates)} fresh news items.")

    for score, entry, link, url_hash, title in candidates[:12]:
        image_url, extra_text = get_article_data(entry, link)
        if not image_url:
            continue
        try:
            print(f"[POSTING] Score {score}: {title}")
            summary = generate_inshorts_summary(title, entry.get("summary", ""), extra_text, models)
            if post_to_telegram(image_url, summary, link):
                state["hashes"].append(url_hash)
                state["titles"].append(title.lower())
                state["last_post_ts"] = int(time.time())
                save_state(state)
                del summary, extra_text, image_url
                gc.collect()
                print("[SUCCESS] News posted & synced to GitHub!")
                return True
        except Exception as e:
            print(f"[RETRY] Failed '{title}': {e}")
            continue
    return False

def format_interval(mins):
    if mins % 60 == 0 and mins >= 60:
        hrs = mins // 60
        return f"{hrs} Hour{'s' if hrs > 1 else ''} ({mins} mins)"
    return f"{mins} Minutes"

def build_control_panel(state):
    mins = int(state.get("interval_mins", 60))
    night = "ON (12 AM - 4 AM)" if state.get("night_pause", True) else "OFF (24x7 Active)"
    last_ts = state.get("last_post_ts", 0)
    elapsed = int((time.time() - last_ts) / 60) if last_ts else 0
    remaining = max(0, mins - elapsed) if last_ts else 0

    text = (
        f"⚙️ <b>Sports Duniya Auto-Bot Control Panel</b>\n\n"
        f"⏱️ <b>Current Interval:</b> {format_interval(mins)}\n"
        f"🌙 <b>Night Pause:</b> {night}\n"
        f"⏳ <b>Next Auto Post In:</b> ~{remaining} mins\n\n"
        f"👇 <i>नीचे बटन दबाकर टाइम बदलें या कस्टम टाइम के लिए</i> <code>/set 25m</code> <i>या</i> <code>/set 2h</code> <i>भेजें:</i>"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "⚡ 15m", "callback_data": "set_15"},
                {"text": "⏱️ 30m", "callback_data": "set_30"},
                {"text": "⏱️ 45m", "callback_data": "set_45"}
            ],
            [
                {"text": "🕐 1 Hr", "callback_data": "set_60"},
                {"text": "🕑 2 Hrs", "callback_data": "set_120"},
                {"text": "🕒 3 Hrs", "callback_data": "set_180"}
            ],
            [
                {"text": "🚀 Post Now", "callback_data": "post_now"},
                {"text": f"🌙 Night Pause: {'ON' if state.get('night_pause', True) else 'OFF'}", "callback_data": "toggle_night"}
            ]
        ]
    }
    return text, keyboard

def send_or_edit_panel(chat_id, state, message_id=None):
    text, keyboard = build_control_panel(state)
    if message_id:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
    requests.post(url, data=payload, timeout=10)

def poll_telegram_commands(state, models):
    offset = state.get("last_update_id", 0) + 1
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=15)
        if not resp.ok:
            return
        updates = resp.json().get("result", [])
        for upd in updates:
            state["last_update_id"] = upd["update_id"]
            
            # 1. अगर यूज़र ने बटन दबाया (Callback Query)
            if "callback_query" in upd:
                cb = upd["callback_query"]
                data = cb.get("data", "")
                chat_id = cb["message"]["chat"]["id"]
                msg_id = cb["message"]["message_id"]
                cb_id = cb["id"]

                if data.startswith("set_"):
                    new_mins = int(data.split("_")[1])
                    state["interval_mins"] = new_mins
                    save_state(state)
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                  data={"callback_query_id": cb_id, "text": f"✅ Interval set to {format_interval(new_mins)}!"})
                    send_or_edit_panel(chat_id, state, msg_id)

                elif data == "toggle_night":
                    state["night_pause"] = not state.get("night_pause", True)
                    save_state(state)
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                  data={"callback_query_id": cb_id, "text": "✅ Night mode updated!"})
                    send_or_edit_panel(chat_id, state, msg_id)

                elif data == "post_now":
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                  data={"callback_query_id": cb_id, "text": "🚀 Sending top news to channel..."})
                    ok = execute_news_post(state, models, force=True)
                    send_or_edit_panel(chat_id, state, msg_id)
                    status_msg = "✅ Top news posted to channel!" if ok else "⚠️ Failed to post news."
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                  data={"chat_id": chat_id, "text": status_msg})

            # 2. अगर यूज़र ने टेक्स्ट कमांड भेजा (/start, /panel, /set 20m, /set 2h)
            elif "message" in upd and "text" in upd["message"]:
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                text = msg["text"].strip().lower()

                if text in ["/start", "/panel", "/settings", "/status", "panel", "menu"]:
                    save_state(state)
                    send_or_edit_panel(chat_id, state)

                elif text.startswith("/set"):
                    m = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?", text)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "m"
                        mins = val * 60 if unit.startswith("h") else val
                        mins = max(5, min(1440, mins))
                        state["interval_mins"] = mins
                        save_state(state)
                        send_or_edit_panel(chat_id, state)
                    else:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                      data={"chat_id": chat_id, "text": "Usage: /set 30m or /set 2h"})
    except Exception:
        pass

def run_cron_scheduler():
    check_secrets()
    models = get_available_models()
    state = load_state()
    start_ts = time.time()

    print(f"[BOT STARTED] Active Interval: {state.get('interval_mins', 60)} mins")
    while (time.time() - start_ts) < 19500:
        execute_news_post(state, models, force=False)
        poll_telegram_commands(state, models)

if __name__ == "__main__":
    run_cron_scheduler()
