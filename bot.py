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
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RAW_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

if "t.me/" in RAW_CHANNEL_ID:
    RAW_CHANNEL_ID = "@" + RAW_CHANNEL_ID.split("t.me/")[-1].strip("/")
elif RAW_CHANNEL_ID and not RAW_CHANNEL_ID.startswith("@") and not RAW_CHANNEL_ID.startswith("-"):
    RAW_CHANNEL_ID = "@" + RAW_CHANNEL_ID

TELEGRAM_CHANNEL_ID = RAW_CHANNEL_ID

RSS_FEEDS = [
    "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
    "http://feeds.bbci.co.uk/sport/rss.xml",
    "https://www.espn.com/espn/rss/news"
]

HASH_FILE = "posted_hashes.json"

def check_secrets():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        print(f"ERROR: Ye GitHub Secrets खाली हैं या सेट नहीं हैं: {', '.join(missing)}")
        sys.exit(1)
    print(f"Target Channel ID: {TELEGRAM_CHANNEL_ID}")

def load_hashes():
    if os.path.exists(HASH_FILE):
        try:
            with open(HASH_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_hash(url_hash, hashes):
    hashes.append(url_hash)
    hashes = hashes[-100:]
    with open(HASH_FILE, "w") as f:
        json.dump(hashes, f)

def get_available_models():
    """API पर उपलब्ध चालू मॉडल्स की लिस्ट निकालता है ताकि 503 आने पर दूसरा मॉडल काम आ सके"""
    models = ["gemini-3.8-flash"]
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        resp = requests.get(url, timeout=15)
        if resp.ok:
            discovered = []
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods and any(k in name for k in ["flash", "gemma", "pro"]):
                    if not any(bad in name for bad in ["2.5", "2.0", "1.5", "tts", "image", "audio", "vision"]):
                        discovered.append(name)
            # Lite और Flash मॉडल्स को सबसे आगे रखें
            discovered.sort(key=lambda x: (0 if "lite" in x else 1 if "flash" in x else 2))
            if discovered:
                models = discovered[:4]
    except Exception as e:
        print(f"Model discovery warning: {e}")
    print(f"Models ready for fallback: {models}")
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
        article_text = " ".join(paragraphs[:5])
    except Exception as e:
        print(f"Article scrape warning: {e}")

    return image_url, article_text

def fallback_inshorts_summary(title, description, extra_text):
    """अगर Google के सभी सर्वर 503 (High Demand) दें, तो खुद 55-60 शब्दों की समरी बनाता है"""
    clean_desc = BeautifulSoup(description or "", "html.parser").get_text().strip()
    combined = f"{clean_desc} {extra_text}".strip()
    words = combined.split()
    
    if len(words) > 58:
        body = " ".join(words[:58]) + "..."
    elif len(words) > 10:
        body = " ".join(words)
    else:
        body = title

    return f"{title}\n\n{body}\n\n#SportsNews #Cricket #Football #DailySports"

def generate_inshorts_summary(title, description, extra_text, models):
    prompt = f"""
    You are a professional sports news editor for an Inshorts-style channel.
    Rewrite the following sports news into English with this exact structure:
    1. First line: A punchy, short headline (maximum 10 words, no markdown symbols or asterisks).
    2. Blank line.
    3. Body: A crisp, objective summary in strictly 50 to 60 words covering key facts, scores, or statements.
    4. Blank line.
    5. Relevant hashtags (e.g., #Cricket #Football #SportsNews).

    News Title: {title}
    News Summary: {description}
    Additional Context: {extra_text[:1000]}
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            print(f"Trying AI model: {model_name}...")
            resp = requests.post(url, json=payload, timeout=45)
            if resp.ok:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            else:
                print(f"Model {model_name} returned {resp.status_code}. Switching model...")
                time.sleep(4)
        except Exception as e:
            print(f"Timeout/Error on {model_name}: {e}. Switching model...")
            time.sleep(4)

    print("All AI models are experiencing high demand (503). Using Smart Fallback Summary...")
    return fallback_inshorts_summary(title, description, extra_text)

def post_to_telegram(image_url, summary_text, source_link):
    headers = {"User-Agent": "Mozilla/5.0"}
    img_resp = requests.get(image_url, headers=headers, timeout=15)
    img_resp.raise_for_status()
    
    image_buffer = BytesIO(img_resp.content)
    image_buffer.name = "news.jpg"
    
    lines = [line.strip() for line in summary_text.split("\n") if line.strip()]
    headline = html.escape(lines[0].replace("*", "").replace("#", ""))
    body = html.escape("\n\n".join(lines[1:]))
    
    caption = f"<b>{headline}</b>\n\n{body}\n\n<a href='{source_link}'>Read Full Story</a>"
    
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": image_buffer}
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML"
    }
    
    res = requests.post(tg_url, data=data, files=files, timeout=20)
    
    image_buffer.close()
    del image_buffer
    del img_resp
    gc.collect()
    
    if not res.ok:
        print(f"Telegram API Error ({res.status_code}): {res.text}")
        return False
    return True

def run_bot():
    check_secrets()
    posted_hashes = load_hashes()
    models = get_available_models()
    
    for feed_url in RSS_FEEDS:
        print(f"Checking feed: {feed_url}")
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            link = entry.link
            url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
            
            if url_hash in posted_hashes:
                continue
                
            image_url, extra_text = get_article_data(entry, link)
            if not image_url:
                continue
                
            try:
                print(f"Preparing summary for: {entry.title}")
                summary = generate_inshorts_summary(
                    entry.title, 
                    entry.get("summary", ""), 
                    extra_text,
                    models
                )
                
                if post_to_telegram(image_url, summary, link):
                    save_hash(url_hash, posted_hashes)
                    del summary, extra_text, image_url
                    gc.collect()
                    print("SUCCESS: News posted to Telegram and memory cleared!")
                    return
            except Exception as e:
                print(f"Failed on article '{entry.title}': {e}")
                continue
                
    print("ERROR: Koi bhi post Telegram par nahi bheji ja saki.")
    sys.exit(1)

if __name__ == "__main__":
    run_bot()
