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
    "http://feeds.bbci.co.uk/sport/rss/rss.xml",
    "https://www.espn.com/espn/rss/news",
    "https://www.skysports.com/rss/12040"
]

PRIORITY_KEYWORDS = [
    "india", "kohli", "rohit", "bumrah", "ipl", "bcci", "world cup", "champions trophy",
    "premier league", "champions league", "real madrid", "barcelona", "man utd", "liverpool",
    "arsenal", "messi", "ronaldo", "haaland", "mbappe", "ufc", "mma", "chess", "gukesh",
    "praggnanandhaa", "hockey", "neeraj chopra", "f1", "final", "record", "breaking", "win"
]

TOP_20_LANGUAGES = {
    "hi": ("🇮🇳 हिंदी", "Simple, natural Hindi (Devanagari script)"),
    "hinglish": ("🔥 Hinglish", "Conversational Hinglish (Hindi in English alphabet)"),
    "en": ("🇬🇧 English", "Crisp, easy-to-read English"),
    "mr": ("🇮🇳 मराठी", "Natural Marathi (Devanagari script)"),
    "bn": ("🇮🇳 বাংলা", "Natural Bengali"),
    "gu": ("🇮🇳 ગુજરાતી", "Natural Gujarati"),
    "ta": ("🇮🇳 தமிழ்", "Natural Tamil"),
    "te": ("🇮🇳 తెలుగు", "Natural Telugu"),
    "kn": ("🇮🇳 ಕನ್ನಡ", "Natural Kannada"),
    "ml": ("🇮🇳 മലയാളം", "Natural Malayalam"),
    "pa": ("🇮🇳 ਪੰਜਾਬੀ", "Natural Punjabi (Gurmukhi)"),
    "or": ("🇮🇳 ଓଡ଼ିଆ", "Natural Odia"),
    "ur": ("🇮🇳 اردو", "Natural Urdu"),
    "as": ("🇮🇳 অসমীয়া", "Natural Assamese"),
    "ne": ("🇳🇵 नेपाली", "Natural Nepali"),
    "sa": ("🇮🇳 संस्कृतम्", "Simple Sanskrit"),
    "es": ("🇪🇸 Español", "Natural Spanish"),
    "fr": ("🇫🇷 Français", "Natural French"),
    "ru": ("🇷🇺 Русский", "Natural Russian"),
    "ar": ("🇸🇦 العربية", "Natural Arabic")
}

POST_SOURCE_TEXT = {}
TRANSLATION_CACHE = {}
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

def save_state(state, push_to_git=False):
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
        "last_update_id": 0,
        "user_langs": {}
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

def clean_text_links(text):
    """अजीब लिंक्स, ट्रैकिंग पैरामीटर्स और एरर यूआरएल को टेक्स्ट से पूरी तरह हटाता है"""
    if not text:
        return ""
    # Remove URL patterns
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # Remove file paths or edgesuite errors
    text = re.sub(r'\b[\w.-]+\.(edgesuite|net|com|org|edu|gov|mil)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'#\w+\.\w+', '', text)
    return text.strip()

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
    models = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
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
                if "generateContent" in methods and any(k in name for k in ["flash", "gemma"]):
                    if not any(bad in name for bad in ["tts", "image", "audio", "vision", "embedding"]):
                        discovered.append(name)
            if discovered:
                models = list(dict.fromkeys(["gemini-3.8-flash"] + discovered))[:5]
    except Exception:
        pass
    return models

def call_ai_text(prompt, models, max_tokens=500):
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
                        "max_tokens": max_tokens,
                        "temperature": 0.4
                    },
                    timeout=15
                )
                if resp.ok:
                    return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                pass

    if GEMINI_API_KEY:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        for model_name in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            try:
                resp = requests.post(url, json=payload, timeout=20)
                if resp.ok:
                    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                pass
    return None

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

def create_fallback_banner(headline_text):
    img = Image.new("RGB", (1280, 720), color=(14, 20, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle([28, 28, 1252, 692], outline=(255, 195, 0), width=6)

    font_title = ImageFont.load_default()
    font_sub = ImageFont.load_default()
    for fp in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    ]:
        if os.path.exists(fp):
            font_title = ImageFont.truetype(fp, 44)
            font_sub = ImageFont.truetype(fp, 34)
            break

    draw.text((75, 85), "⚡ BREAKING SPORTS UPDATE", font=font_sub, fill=(255, 195, 0))
    clean_h = re.sub(r"[^\p{L}\p{N}\s.,!?'\":;-]", "", headline_text).strip() or "Latest Sports News Update"
    words = clean_h.split()
    lines = [" ".join(words[i:i+6]) for i in range(0, len(words), 6)]
    y_pos = 210
    for ln in lines[:5]:
        draw.text((75, y_pos), ln, font=font_title, fill=(255, 255, 255))
        y_pos += 68

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf.read()

def download_image_bypass_403(image_url, source_link=""):
    if not image_url:
        return None
    clean_url = image_url.split("?")[0] if "ndtvimg.com" in image_url else image_url
    encoded_full = urllib.parse.quote(image_url, safe="")
    encoded_clean = urllib.parse.quote(clean_url, safe="")
    stripped_scheme = re.sub(r"^https?://", "", clean_url)

    candidate_urls = [
        f"https://wsrv.nl/?url={encoded_clean}&w=1280&output=jpg",
        f"https://wsrv.nl/?url={encoded_full}&output=jpg",
        f"https://i0.wp.com/{stripped_scheme}",
        clean_url,
        image_url
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://www.google.com/"
    }

    for try_url in candidate_urls:
        try:
            r = requests.get(try_url, headers=headers, timeout=10)
            if r.ok and len(r.content) > 2500:
                return r.content
        except Exception:
            continue
    return None

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
        f"💬 What's your take on this? Comment below!\n\n"
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
    Line 2: A 1-line engaging fan hook starting with 💬 (e.g., "💬 Can India bounce back in the next match? Comment below!").
    Blank line.
    Line 3: 4 to 5 high-search hashtags (e.g., #Cricket #INDvsPAK #RohitSharma #Football #SportsDuniya).

    News Title: {title}
    News Summary: {description}
    Additional Context: {extra_text[:1200]}
    """
    res = call_ai_text(prompt, models, max_tokens=450)
    if res:
        return res
    return fallback_inshorts_summary(title, description, extra_text)

def post_to_telegram(image_url, summary_text, source_link=""):
    lines = [line.strip() for line in summary_text.split("\n") if line.strip()]
    raw_headline = lines[0].replace("*", "").replace("#", "") if lines else "Sports News"

    img_bytes = download_image_bypass_403(image_url, source_link)
    if not img_bytes:
        img_bytes = create_fallback_banner(raw_headline)

    branded_buffer = watermark_image_in_ram(img_bytes)

    headline = html.escape(raw_headline)
    body = html.escape("\n\n".join(lines[1:]))

    caption = (
        f"<b>{headline}</b>\n\n"
        f"{body}\n\n"
        f"⚡ <b>{BRAND_TAG}</b>"
    )

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": branded_buffer}
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

# =========================================================
# LINKED GROUP AUTO-HINDI + PERSONAL TOP-20 LANGUAGE SYSTEM
# =========================================================
def translate_sports_post(raw_text, lang_code, models, short_for_popup=False):
    cleaned_src = clean_text_links(raw_text)
    cache_key = f"{hashlib.md5(cleaned_src.encode('utf-8')).hexdigest()[:10]}_{lang_code}_{'pop' if short_for_popup else 'full'}"
    if cache_key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[cache_key]

    lang_label, lang_desc = TOP_20_LANGUAGES.get(lang_code, TOP_20_LANGUAGES["hi"])
    if short_for_popup:
        prompt = (
            f"Translate and summarize this sports news into {lang_desc} in strictly under 175 characters "
            f"(so it fits inside a Telegram popup alert). Keep key player/team names and score intact. "
            f"No markdown or asterisks:\n\n{cleaned_src}"
        )
        out = call_ai_text(prompt, models, max_tokens=150)
        if out:
            out = out.replace("*", "").replace("#", "").strip()[:190]
            TRANSLATION_CACHE[cache_key] = out
            return out
        return cleaned_src[:190]

    prompt = (
        f"Translate this sports news post into {lang_desc}.\n"
        f"Rules:\n"
        f"1. Line 1: Keep the sport emoji + catchy translated headline.\n"
        f"2. Blank line, then full accurate translation of the news paragraph.\n"
        f"3. Do NOT use asterisks (*) or markdown symbols.\n\n"
        f"Text:\n{cleaned_src}"
    )
    out = call_ai_text(prompt, models, max_tokens=500)
    if out:
        out = out.replace("*", "").strip()
        TRANSLATION_CACHE[cache_key] = out
        return out
    return cleaned_src

def build_group_translation_keyboard(source_msg_id):
    return {
        "inline_keyboard": [
            [
                {"text": "🌐 मेरी भाषा में पढ़ें (Only Me)", "callback_data": f"mytr_{source_msg_id}"},
                {"text": "⚙️ Language (Top 20)", "callback_data": f"langmenu_{source_msg_id}"}
            ]
        ]
    }

def build_top_20_language_keyboard(source_msg_id=0):
    rows = []
    items = list(TOP_20_LANGUAGES.items())
    for i in range(0, len(items), 3):
        row = []
        for code, (label, _) in items[i:i+3]:
            row.append({"text": label, "callback_data": f"setlang_{code}_{source_msg_id}"})
        rows.append(row)
    if source_msg_id:
        rows.append([{"text": "❌ बंद करें (Close Menu)", "callback_data": f"closelang_{source_msg_id}"}])
    return {"inline_keyboard": rows}

def handle_linked_channel_post_in_group(msg, models):
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    raw_text = (msg.get("caption") or msg.get("text") or "").strip()
    if len(raw_text) < 15:
        return

    clean_source = clean_text_links(raw_text)
    POST_SOURCE_TEXT[str(msg_id)] = clean_source

    hindi_text = translate_sports_post(clean_source, "hi", models, short_for_popup=False)
    lines = [l.strip() for l in hindi_text.split("\n") if l.strip()]
    headline = html.escape(lines[0]) if lines else "🇮🇳 हिंदी समाचार"
    body = html.escape("\n\n".join(lines[1:])) if len(lines) > 1 else ""

    reply_html = (
        f"🇮🇳 <b>{headline}</b>\n\n"
        f"{body}\n\n"
        f"<i>💡 अपनी भाषा (Top 20) में अकेले पढ़ने के लिए नीचे क्लिक करें या /settings लिखें:</i>"
    )

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": reply_html[:4000],
            "parse_mode": "HTML",
            "reply_to_message_id": msg_id,
            "reply_markup": json.dumps(build_group_translation_keyboard(msg_id))
        },
        timeout=15
    )

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
                title = entry.get("title", "")
                if not link or not title:
                    continue
                url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
                if is_duplicate(url_hash, title, state):
                    continue
                score = score_entry(entry, idx)
                candidates.append((score, entry, link, url_hash, title))
        except Exception as e:
            print(f"[RSS SKIP] {feed_url}: {e}")
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    print(f"[SCAN] Found {len(candidates)} fresh news items.")

    for score, entry, link, url_hash, title in candidates[:12]:
        image_url, extra_text = get_article_data(entry, link)
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
                print("[SUCCESS] News posted & synced to GitHub in background!")
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
        f"🌐 <b>Linked Group Auto-Hindi + Top 20 Langs:</b> ON 🟢\n\n"
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
                user_id = str(cb["from"]["id"])
                user_name = cb["from"].get("first_name", "Member")
                cb_id = cb["id"]

                if data.startswith("mytr_"):
                    src_id = data.split("_")[1]
                    user_lang = state.get("user_langs", {}).get(user_id)

                    if not user_lang:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            data={
                                "callback_query_id": cb_id,
                                "text": "⚙️ कृपया पहले पास वाले '⚙️ Language (Top 20)' बटन पर क्लिक करके अपनी भाषा चुनें (या ग्रुप में /settings लिखें)!",
                                "show_alert": True
                            },
                            timeout=8
                        )
                        continue

                    raw_src = POST_SOURCE_TEXT.get(src_id)
                    if not raw_src and "reply_to_message" in cb["message"]:
                        rep = cb["message"]["reply_to_message"]
                        raw_src = (rep.get("caption") or rep.get("text") or "").strip()
                    if not raw_src:
                        raw_src = (cb["message"].get("text") or "").strip()

                    lang_label, _ = TOP_20_LANGUAGES.get(user_lang, TOP_20_LANGUAGES["hi"])
                    popup_tr = translate_sports_post(raw_src, user_lang, models, short_for_popup=True)

                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={
                            "callback_query_id": cb_id,
                            "text": f"{lang_label}:\n{popup_tr}"[:198],
                            "show_alert": True
                        },
                        timeout=8
                    )

                elif data.startswith("langmenu_"):
                    src_id = data.split("_")[1]
                    curr_code = state.get("user_langs", {}).get(user_id, "hi")
                    curr_label, _ = TOP_20_LANGUAGES.get(curr_code, TOP_20_LANGUAGES["hi"])
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": f"आपकी वर्तमान भाषा: {curr_label}"},
                        timeout=8
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                        data={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "reply_markup": json.dumps(build_top_20_language_keyboard(src_id))
                        },
                        timeout=8
                    )

                elif data.startswith("setlang_"):
                    parts = data.split("_")
                    chosen_code = parts[1]
                    src_id = parts[2] if len(parts) > 2 else "0"

                    if "user_langs" not in state:
                        state["user_langs"] = {}
                    state["user_langs"][user_id] = chosen_code
                    save_state(state, push_to_git=True)
                    lang_label, _ = TOP_20_LANGUAGES.get(chosen_code, TOP_20_LANGUAGES["hi"])

                    if src_id != "0":
                        raw_src = POST_SOURCE_TEXT.get(src_id, "")
                        if not raw_src and "reply_to_message" in cb["message"]:
                            rep = cb["message"]["reply_to_message"]
                            raw_src = (rep.get("caption") or rep.get("text") or "").strip()

                        popup_tr = translate_sports_post(raw_src, chosen_code, models, short_for_popup=True) if raw_src else ""
                        alert_msg = f"✅ {user_name}, आपकी भाषा {lang_label} सेट हो गई!\n\n{popup_tr}"[:198]
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            data={"callback_query_id": cb_id, "text": alert_msg, "show_alert": True},
                            timeout=8
                        )
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            data={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "reply_markup": json.dumps(build_group_translation_keyboard(src_id))
                            },
                            timeout=8
                        )
                    else:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            data={
                                "callback_query_id": cb_id,
                                "text": f"✅ {user_name}, आपकी भाषा '{lang_label}' सेव हो गई! अब किसी भी पोस्ट के नीचे '🌐 मेरी भाषा में पढ़ें' दबाएं।",
                                "show_alert": True
                            },
                            timeout=8
                        )
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                            data={"chat_id": chat_id, "message_id": msg_id},
                            timeout=8
                        )

                elif data.startswith("closelang_"):
                    src_id = data.split("_")[1]
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id},
                        timeout=5
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                        data={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "reply_markup": json.dumps(build_group_translation_keyboard(src_id))
                        },
                        timeout=8
                    )

                elif data.startswith("set_"):
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
                        data={"callback_query_id": cb_id, "text": "🚀 न्यूज़ तैयार होकर चैनल पर जा रही है..."},
                        timeout=8
                    )
                    ok = execute_news_post(state, models, force=True)
                    send_or_edit_panel(chat_id, state, msg_id)
                    status_msg = "✅ Top news posted to channel!" if ok else "⚠️ कोई नई न्यूज़ नहीं मिली।"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": chat_id, "text": status_msg},
                        timeout=8
                    )

            elif "message" in upd:
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                chat_type = msg["chat"].get("type", "private")

                # Linked Channel की इंग्लिश पोस्ट ग्रुप में आते ही ऑटो-हिंदी रिप्लाई
                if msg.get("is_automatic_forward") or (msg.get("sender_chat", {}).get("type") == "channel" and chat_type in ["group", "supergroup"]):
                    threading.Thread(target=handle_linked_channel_post_in_group, args=(msg, models), daemon=True).start()
                    continue

                if "text" not in msg:
                    continue

                text = msg["text"].strip()
                low_text = text.lower()

                if low_text.startswith("/settings") or low_text.startswith("/lang"):
                    if chat_type == "private":
                        send_or_edit_panel(chat_id, state)
                        continue

                    user_id = str(msg.get("from", {}).get("id", ""))
                    user_name = html.escape(msg.get("from", {}).get("first_name", "Member"))
                    curr_code = state.get("user_langs", {}).get(user_id, "hi")
                    curr_label, _ = TOP_20_LANGUAGES.get(curr_code, TOP_20_LANGUAGES["hi"])

                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                        data={"chat_id": chat_id, "message_id": msg["message_id"]},
                        timeout=5
                    )

                    lang_msg = (
                        f"🌐 <b>{user_name}, अपनी पसंदीदा भाषा चुनें (Top 20 Languages):</b>\n"
                        f"• आपकी वर्तमान भाषा: <b>{curr_label}</b>\n\n"
                        f"<i>👇 नीचे अपनी भाषा पर क्लिक करें—यह मेनू अपने आप हट जाएगा और पोस्ट केवल आपको आपकी भाषा में दिखेगी:</i>"
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={
                            "chat_id": chat_id,
                            "text": lang_msg,
                            "parse_mode": "HTML",
                            "reply_markup": json.dumps(build_top_20_language_keyboard(0))
                        },
                        timeout=8
                    )

                elif low_text in ["/start", "/panel", "/status", "panel", "menu"]:
                    send_or_edit_panel(chat_id, state)

                elif low_text.startswith("/set"):
                    m = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?", low_text)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "m"
                        mins = val * 60 if unit.startswith("h") else val
                        mins = max(5, min(1440, mins))
                        state["interval_mins"] = mins
                        send_or_edit_panel(chat_id, state)
                        save_state(state, push_to_git=True)
    except Exception:
        time.sleep(3)

def run_cron_scheduler():
    check_secrets()
    models = get_available_models()
    state = load_state()
    start_ts = time.time()
    last_log_ts = 0

    print(f"[BOT STARTED] Active Interval: {state.get('interval_mins', 60)} mins | Clean Link Mode ON")
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
