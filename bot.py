import os
import sys
import gc
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
    os.system('git commit -m "Update posted news hashes" >/dev/null 2>&1 || true')
    os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")
    os.system("git push origin main >/dev/null 2>&1 || true")

def load_state():
    sync_git_pull()
    if os.path.exists(HASH_FILE):
        try:
            with open(HASH_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"hashes": data, "titles": [], "last_post_ts": 0}
                if "last_post_ts" not in data:
                    data["last_post_ts"] = 0
                return data
        except Exception:
            pass
    return {"hashes": [], "titles": [], "last_post_ts": 0}

def save_state(url_hash, title, state):
    state["hashes"].append(url_hash)
    state["titles"].append(title.lower())
    state["hashes"] = state["hashes"][-150:]
    state["titles"] = state["titles"][-100:]
    state["last_post_ts"] = int(time.time())
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
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

def post_to_telegram(image_url, summary_text):
    headers = {"User-Agent": "Mozilla/5.0"}
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

def execute_hourly_job(models):
    ist_now = get_ist_now()
    print(f"\n[CRON CHECK] IST Time: {ist_now.strftime('%Y-%m-%d %I:%M %p')}")

    # रात 12:00 AM से 4:00 AM IST तक ऑटो-स्लीप
    if 0 <= ist_now.hour < 4:
        print("[NIGHT MODE] 12 AM - 4 AM IST active. Skipping post.")
        return

    state = load_state()
    elapsed_mins = (int(time.time()) - state.get("last_post_ts", 0)) / 60
    if elapsed_mins < 52:
        print(f"[WAIT] Last post was {int(elapsed_mins)} mins ago. Next post in {int(55 - elapsed_mins)} mins.")
        return

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
            if post_to_telegram(image_url, summary):
                save_state(url_hash, title, state)
                del summary, extra_text, image_url
                gc.collect()
                print("[SUCCESS] News posted & hash synced to GitHub!")
                return
        except Exception as e:
            print(f"[RETRY] Failed '{title}': {e}")
            continue

def run_cron_scheduler():
    """यह इन-बिल्ट क्रॉन इंजन हर 1 घंटे में अपने आप न्यूज़ पोस्ट करता रहेगा"""
    check_secrets()
    models = get_available_models()
    start_ts = time.time()

    # 5.5 घंटे का नॉन-स्टॉप क्रॉन लूप (खत्म होते ही GitHub अगला लूप शुरू कर देगा)
    while (time.time() - start_ts) < 19500:
        execute_hourly_job(models)
        print("[CRON SLEEP] Waiting 15 minutes for next time check...")
        time.sleep(900)

if __name__ == "__main__":
    run_cron_scheduler()
