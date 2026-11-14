import os
import sys
import gc
import re
import time
import html
import json
import hashlib
import threading
import urllib.parse
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

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

# हाई-ट्रेंडिंग स्पोर्ट्स कीवर्ड्स (जिन खबरों को सबसे पहले प्राथमिकता मिलेगी)
PRIORITY_KEYWORDS = [
    "india", "kohli", "rohit", "bumrah", "pant", "gill", "hardik", "dhoni", "ipl", "bcci",
    "world cup", "champions trophy", "test", "t20", "odi", "icc", "asia cup", "border-gavaskar",
    "premier league", "champions league", "real madrid", "barcelona", "man utd", "liverpool",
    "arsenal", "man city", "messi", "ronaldo", "haaland", "mbappe", "ufc", "mma", "chess",
    "gukesh", "praggnanandhaa", "neeraj chopra", "olympics", "f1", "final", "record", "win"
]

# फालतू, बोरिंग या स्पैम खबरों को रोकने की लिस्ट
JUNK_KEYWORDS = [
    "dream11", "fantasy", "prediction", "betting", "odds", "promo code", "horoscope",
    "quiz:", "wordle", "connections", "sponsored", "shopping", "deal of the day",
    "live streaming: how to watch", "where to watch free"
]

HASH_FILE = "posted_hashes.json"
git_lock = threading.Lock()

def get_ist_now():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_tz)

def check_secrets():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID")
        sys.exit(1)
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        print("ERROR: Both GROQ_API_KEY and GEMINI_API_KEY are missing!")
        sys.exit(1)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            data={"drop_pending_updates": False},
            timeout=10
        )
    except Exception:
        pass
    print(f"AI Engines Ready -> Groq: {'YES' if GROQ_API_KEY else 'NO'} | Gemini: {'YES' if GEMINI_API_KEY else 'NO'}")

def sync_git_pull():
    os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")

def _background_git_push():
    with git_lock:
        os.system('git config --global user.name "github-actions[bot]"')
        os.system('git config --global user.email "github-actions[bot]@users.noreply.github.com"')
        os.system("git add posted_hashes.json")
        os.system('git commit -m "Update bot state & settings" >/dev/null 2>&1 || true')
        os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")
        os.system("git push origin main >/dev/null 2>&1 || true")

def save_state(state, push_to_git=True):
    state["hashes"] = state["hashes"][-150:]
    state["titles"] = state["titles"][-100:]
    try:
        with open(HASH_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
    if push_to_git:
        threading.Thread(target=_background_git_push, daemon=True).start()

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

def sanitize_raw_text(text):
    """वेबसाइट के एरर कोड (edgesuite.net, Reference #), फालतू लिंक्स और कचरे को साफ करता है"""
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    # Remove Akamai / Cloudflare error strings
    if any(err in text.lower() for err in ["edgesuite.net", "reference #", "access denied", "403 forbidden", "enable javascript"]):
        text = re.sub(r"reference\s*#[\w\d.]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"access denied.*", "", text, flags=re.IGNORECASE)
    # Remove all raw URLs and domain artefacts
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\b[\w.-]+\.(edgesuite|net|com|org|in|co\.uk)\S*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_duplicate(url_hash, title, state):
    if url_hash in state["hashes"]:
        return True
    clean_title = title.lower()
    for old_title in state["titles"]:
        if SequenceMatcher(None, clean_title, old_title).ratio() > 0.65:
            return True
    return False

def is_junk_news(title, summary):
    combined = f"{title} {summary}".lower()
    return any(junk in combined for junk in JUNK_KEYWORDS)

def score_entry(entry, index):
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    if is_junk_news(title, summary):
        return -999

    text = f"{title} {summary}".lower()
    score = max(0, 20 - index)
    for kw in PRIORITY_KEYWORDS:
        if kw in text:
            score += 12
    # क्रिकेट और भारतीय खेल की बड़ी खबरों को अतिरिक्त बूस्ट
    if any(k in text for k in ["india", "kohli", "rohit", "bumrah", "ipl", "bcci", "world cup"]):
        score += 15
    return score

def get_available_models():
    """सभी चालू Gemini Flash मॉडल्स को अनलॉक रखता है (2.5, 2.0, 1.5, 3.8)"""
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest"]
    if not GEMINI_API_KEY:
        return models
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.ok:
            discovered = []
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods and ("flash" in name or "gemma" in name):
                    if not any(bad in name for bad in ["tts", "image", "audio", "vision", "embedding"]):
                        discovered.append(name)
            if discovered:
                models = list(dict.fromkeys(discovered + models))[:6]
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
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")

            if not image_url:
                og_image = soup.find("meta", property="og:image")
                if og_image and og_image.get("content"):
                    image_url = og_image["content"]

            paragraphs = []
            for p in soup.find_all("p"):
                p_txt = sanitize_raw_text(p.get_text())
                if len(p_txt) > 45 and not any(bad in p_txt.lower() for bad in ["cookies", "subscribe", "newsletter", "all rights reserved"]):
                    paragraphs.append(p_txt)
            article_text = " ".join(paragraphs[:7])
    except Exception:
        pass

    return image_url, sanitize_raw_text(article_text)

def download_real_image(image_url, source_link=""):
    """NDTV और अन्य साइटों के 403 Forbidden को बायपास करके केवल असली HD फोटो डाउनलोड करता है"""
    if not image_url:
        return None
    clean_url = image_url.split("?")[0] if "ndtvimg.com" in image_url else image_url
    encoded_clean = urllib.parse.quote(clean_url, safe="")
    encoded_full = urllib.parse.quote(image_url, safe="")
    stripped_scheme = re.sub(r"^https?://", "", clean_url)

    candidate_urls = [
        image_url,
        f"https://wsrv.nl/?url={encoded_clean}&w=1280&output=jpg",
        f"https://wsrv.nl/?url={encoded_full}&output=jpg",
        f"https://i0.wp.com/{stripped_scheme}"
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": source_link if source_link else "https://www.google.com/"
    }

    for try_url in candidate_urls:
        try:
            r = requests.get(try_url, headers=headers, timeout=12)
            if r.ok and len(r.content) > 4000:
                return r.content
        except Exception:
            continue
    return None

def watermark_image_in_ram(image_bytes):
    """असली फोटो के कोने में @SportsDuniyaOfficial का प्रीमियम ब्रांड टैग लगाता है"""
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
    clean_title = sanitize_raw_text(title)
    clean_desc = sanitize_raw_text(description)
    clean_extra = sanitize_raw_text(extra_text)
    combined = f"{clean_desc} {clean_extra}".strip()

    if len(combined) < 40 or combined.lower() == clean_title.lower():
        body = (
            f"{clean_title}. This developing story has caught the attention of sports fans worldwide "
            f"as key players and teams prepare for the upcoming high-stakes fixtures."
        )
    else:
        words = combined.split()
        body = " ".join(words[:60]) + ("." if not combined.endswith(".") else "")

    return (
        f"🔥 {clean_title}\n"
        f"⚡ Big Update from the World of Sports\n\n"
        f"{body}\n\n"
        f"💬 What's your take on this? Drop your thoughts in the comments!\n\n"
        f"#SportsNews #Cricket #Football #SportsDuniya"
    )

def generate_inshorts_summary(title, description, extra_text, models):
    clean_title = sanitize_raw_text(title)
    clean_desc = sanitize_raw_text(description)
    clean_extra = sanitize_raw_text(extra_text)

    prompt = f"""
    You are the Senior Sports Editor of '{BRAND_TAG}', a premium, human-curated Inshorts-style Telegram sports channel.
    Write a highly engaging, 100% natural human-sounding, SEO-optimized English sports post.
    If the provided summary is brief, enrich it using accurate background context (tournament name, player role, or recent rivalry context) so the reader gets the complete story without needing to click any external link.

    Follow this EXACT structure (no markdown asterisks ** or labels like "Line 1"):

    Line 1: [Relevant Sport Emoji: 🏏/⚽/🥊/♟️/🏎️/🎾] + [Main Punchy Headline in 8-11 words with full Player/Team name]
    Line 2: ⚡ [1-line sharp sub-headline highlighting the key stat, quote, or impact in 7-10 words]

    [Blank Line]

    Paragraph: A crisp, human-written Inshorts summary of strictly 55 to 65 words. Pack the biggest update, score, or statement in the first sentence, followed by essential context. Never include URLs, website names, or robotic phrases.

    [Blank Line]

    Comment Hook: A specific 1-line fan debate question starting with 💬 ending with "Comment below!"

    [Blank Line]

    Hashtags: 5 high-search organic SEO hashtags (e.g., #Cricket #RohitSharma #INDvsAUS #PremierLeague #SportsDuniya).

    News Headline: {clean_title}
    Feed Summary: {clean_desc}
    Article Details: {clean_extra[:1400]}
    """

    # 1. Try Groq AI Models
    if GROQ_API_KEY:
        for groq_model in [
            "llama-3.3-70b-versatile",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "llama-3.1-8b-instant"
        ]:
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": groq_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 450,
                        "temperature": 0.45
                    },
                    timeout=18
                )
                if resp.ok:
                    out = resp.json()["choices"][0]["message"]["content"].strip()
                    print(f"[AI SUCCESS] Generated via Groq ({groq_model})")
                    return out
            except Exception as e:
                print(f"[GROQ WARN] {groq_model}: {e}")

    # 2. Try Gemini Flash Models
    if GEMINI_API_KEY:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        for model_name in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            try:
                resp = requests.post(url, json=payload, timeout=22)
                if resp.ok:
                    out = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    print(f"[AI SUCCESS] Generated via Gemini ({model_name})")
                    return out
            except Exception:
                continue

    print("[AI FALLBACK] Using sanitized smart summary")
    return fallback_inshorts_summary(clean_title, clean_desc, clean_extra)

def post_to_telegram(image_url, summary_text, source_link=""):
    img_bytes = download_real_image(image_url, source_link)
    if not img_bytes:
        print("[IMAGE SKIP] Real image could not be downloaded, trying next trending article...")
        return False

    branded_buffer = watermark_image_in_ram(img_bytes)

    raw_lines = [line.strip().replace("**", "").replace("*", "") for line in summary_text.split("\n") if line.strip()]
    # Clean any accidental URLs from lines
    lines = [re.sub(r"https?://\S+|www\.\S+", "", l).strip() for l in raw_lines if l.strip()]

    # Top 2-Line Bold Heading + Inshorts Body
    if len(lines) >= 3 and (lines[1].startswith("⚡") or len(lines[1].split()) <= 14):
        top_heading = f"<b>{html.escape(lines[0])}</b>\n<b>{html.escape(lines[1])}</b>"
        body = html.escape("\n\n".join(lines[2:]))
    else:
        top_heading = f"<b>{html.escape(lines[0])}</b>"
        body = html.escape("\n\n".join(lines[1:]))

    caption = (
        f"{top_heading}\n\n"
        f"{body}\n\n"
        f"⚡ <b>{BRAND_TAG}</b>"
    )

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": branded_buffer}
    # Inline button removed so Linked Group's native "Leave a comment" bar works!
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML"
    }

    res = requests.post(tg_url, data=data, files=files, timeout=20)

    branded_buffer.close()
    del branded_buffer
    del img_bytes
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
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}

    for feed_url in RSS_FEEDS:
        try:
            r = requests.get(feed_url, headers=req_headers, timeout=8)
            if not r.ok:
                continue
            feed = feedparser.parse(r.content)
            for idx, entry in enumerate(feed.entries[:15]):
                link = entry.get("link", "")
                title = sanitize_raw_text(entry.get("title", ""))
                if not link or not title:
                    continue
                url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
                if is_duplicate(url_hash, title, state):
                    continue
                score = score_entry(entry, idx)
                if score < 0:
                    continue
                candidates.append((score, entry, link, url_hash, title))
        except Exception as e:
            print(f"[RSS SKIP] {feed_url}: {e}")
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    print(f"[SCAN] Found {len(candidates)} trending sports news items.")

    for score, entry, link, url_hash, title in candidates[:15]:
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
                save_state(state, push_to_git=True)
                del summary, extra_text, image_url
                gc.collect()
                print("[SUCCESS] Trending news posted with @SportsDuniyaOfficial watermark!")
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
        f"⏳ <b>Next Auto Post In:</b> ~{remaining} mins\n"
        f"💬 <b>Channel Mode:</b> Clean Inshorts + Native Comments ON 🟢\n\n"
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
        resp = requests.get(url, params={"offset": offset, "timeout": 8}, timeout=12)
        if not resp.ok:
            time.sleep(3)
            return
        updates = resp.json().get("result", [])
        if not updates:
            return

        for upd in updates:
            state["last_update_id"] = upd["update_id"]
            save_state(state, push_to_git=False)

            if "callback_query" in upd:
                cb = upd["callback_query"]
                data = cb.get("data", "")
                chat_id = cb["message"]["chat"]["id"]
                msg_id = cb["message"]["message_id"]
                cb_id = cb["id"]

                if data.startswith("set_"):
                    new_mins = int(data.split("_")[1])
                    state["interval_mins"] = new_mins
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": f"✅ Interval set to {format_interval(new_mins)}!"},
                        timeout=8
                    )
                    send_or_edit_panel(chat_id, state, msg_id)
                    save_state(state, push_to_git=True)

                elif data == "toggle_night":
                    state["night_pause"] = not state.get("night_pause", True)
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "✅ Night mode updated!"},
                        timeout=8
                    )
                    send_or_edit_panel(chat_id, state, msg_id)
                    save_state(state, push_to_git=True)

                elif data == "post_now":
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "🚀 Sending trending news to channel..."},
                        timeout=8
                    )
                    ok = execute_news_post(state, models, force=True)
                    send_or_edit_panel(chat_id, state, msg_id)
                    status_msg = "✅ Top trending news posted to channel!" if ok else "⚠️ Failed to post news."
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": chat_id, "text": status_msg},
                        timeout=8
                    )

            elif "message" in upd and "text" in upd["message"]:
                msg = upd["message"]
                if msg["chat"].get("type", "private") != "private":
                    continue
                chat_id = msg["chat"]["id"]
                text = msg["text"].strip().lower()

                if text in ["/start", "/panel", "/settings", "/status", "panel", "menu"]:
                    send_or_edit_panel(chat_id, state)

                elif text.startswith("/set"):
                    m = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?", text)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "m"
                        mins = val * 60 if unit.startswith("h") else val
                        mins = max(5, min(1440, mins))
                        state["interval_mins"] = mins
                        send_or_edit_panel(chat_id, state)
                        save_state(state, push_to_git=True)
                    else:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            data={"chat_id": chat_id, "text": "Usage: /set 30m or /set 2h"},
                            timeout=8
                        )
    except Exception:
        time.sleep(3)

def run_cron_scheduler():
    check_secrets()
    models = get_available_models()
    state = load_state()
    start_ts = time.time()
    last_log_ts = 0

    print(f"[BOT STARTED] Active Interval: {state.get('interval_mins', 60)} mins | Trending Inshorts Mode")
    while (time.time() - start_ts) < 19500:
        execute_news_post(state, models, force=False)

        if time.time() - last_log_ts > 120:
            mins = int(state.get("interval_mins", 60))
            last_ts = state.get("last_post_ts", 0)
            elapsed = int((time.time() - last_ts) / 60) if last_ts else mins
            rem = max(0, mins - elapsed)
            print(f"[LIVE TIMER] IST: {get_ist_now().strftime('%I:%M %p')} | Last post: {elapsed}m ago | Next post in: {rem}m")
            last_log_ts = time.time()

        poll_telegram_commands(state, models)

if __name__ == "__main__":
    run_cron_scheduler()
