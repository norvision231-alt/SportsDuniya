import os
import gc
import json
import hashlib
import requests
import feedparser
from io import BytesIO
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel_username")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_FREE_GEMINI_API_KEY")

RSS_FEEDS = [
    "https://www.espn.com/espn/rss/news",
    "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
    "http://feeds.bbci.co.uk/sport/rss.xml"
]

HASH_FILE = "posted_hashes.json"

def load_hashes():
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r") as f:
            return json.load(f)
    return []

def save_hash(url_hash, hashes):
    hashes.append(url_hash)
    hashes = hashes[-100:]
    with open(HASH_FILE, "w") as f:
        json.dump(hashes, f)

def get_article_data(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        og_image = soup.find("meta", property="og:image")
        image_url = og_image["content"] if og_image else None
        
        paragraphs = [p.get_text() for p in soup.find_all("p")[:5]]
        article_text = " ".join(paragraphs)
        
        return image_url, article_text
    except Exception:
        return None, ""

def generate_inshorts_summary(title, description, extra_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    You are a professional sports news editor for an Inshorts-style channel.
    Rewrite the following sports news into English with this exact structure:
    1. First line: A punchy, short headline (maximum 10 words, no markdown symbols).
    2. Blank line.
    3. Body: A crisp, objective summary in strictly 50 to 60 words covering key facts, scores, or statements.
    4. Blank line.
    5. Relevant hashtags (e.g., #Cricket #Football #SportsNews).

    News Title: {title}
    News Summary: {description}
    Additional Context: {extra_text[:1000]}
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

def post_to_telegram(image_url, summary_text, source_link):
    img_resp = requests.get(image_url, timeout=10)
    img_resp.raise_for_status()
    
    image_buffer = BytesIO(img_resp.content)
    image_buffer.name = "news.jpg"
    
    lines = summary_text.split("\n")
    headline = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    
    caption = f"<b>{headline}</b>\n\n{body}\n\n<a href='{source_link}'>Read Full Story</a>"
    
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": image_buffer}
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML"
    }
    
    res = requests.post(tg_url, data=data, files=files, timeout=15)
    
    image_buffer.close()
    del image_buffer
    del img_resp
    gc.collect()
    
    return res.ok

def run_bot():
    posted_hashes = load_hashes()
    
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            link = entry.link
            url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
            
            if url_hash in posted_hashes:
                continue
                
            image_url, extra_text = get_article_data(link)
            if not image_url:
                continue
                
            try:
                summary = generate_inshorts_summary(
                    entry.title, 
                    entry.get("summary", ""), 
                    extra_text
                )
                
                success = post_to_telegram(image_url, summary, link)
                if success:
                    save_hash(url_hash, posted_hashes)
                    del summary, extra_text, image_url
                    gc.collect()
                    print("Posted successfully and cleared all news data!")
                    return
            except Exception as e:
                print(f"Skipped due to error: {e}")
                continue

if __name__ == "__main__":
    run_bot()
