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
from collections import Counter
from io import BytesIO
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RAW_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

BRAND_TAG = "@SportsDuniyaOfficial"

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
    "india", "kohli", "rohit", "bumrah", "pant", "gill", "hardik", "dhoni", "suryakumar",
    "ipl", "bcci", "world cup", "champions trophy", "test", "t20", "odi", "icc", "asia cup",
    "border-gavaskar", "wpl", "rcb", "csk", "mi", "kkr", "pakistan", "australia", "england",
    "premier league", "champions league", "real madrid", "barcelona", "man utd", "liverpool",
    "arsenal", "man city", "messi", "ronaldo", "haaland", "mbappe", "ufc", "mma", "chess",
    "gukesh", "praggnanandhaa", "neeraj chopra", "olympics", "f1", "final", "record", "win",
    "century", "hat-trick", "injured", "squad", "ranking", "ban", "transfer", "champion"
]

JUNK_KEYWORDS = [
    "dream11", "fantasy", "prediction", "betting", "odds", "promo code", "horoscope",
    "quiz:", "wordle", "connections", "sponsored", "shopping", "deal of the day",
    "live streaming", "how to watch", "where to watch", "when and where", "tv channel",
    "ticket", "weather report", "pitch report"
]

COMPETITOR_BRANDS = [
    "ndtv sports", "ndtv", "espncricinfo", "cricinfo", "espn", "sky sports", "skysports",
    "bbc sport", "bbc", "reuters", "pti", "associated press", "ani", "cricbuzz",
    "times of india", "hindustan times", "indian express", "sportskeeda", "insidesport"
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
    state["hashes"] = state["hashes"][-200:]
    state["titles"] = state["titles"][-150:]
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

def strip_links_and_brands(text):
    """किसी भी बाहरी लिंक, रीडायरेक्ट, एरर कोड और दूसरी मीडिया कंपनियों के नाम को पूरी तरह हटाता है"""
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)

    # Remove server error strings (Akamai / Cloudflare / Reference #)
    text = re.sub(r"reference\s*#[\w\d.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"access denied.*", "", text, flags=re.IGNORECASE)

    # Remove all URLs, domains, and @handles (except our own brand)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\b[\w.-]+\.(edgesuite|net|com|org|in|co\.uk|tv|me|io)\S*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@(?!SportsDuniyaOfficial\b)\w+", "", text, flags=re.IGNORECASE)

    # Remove competitor brand names and trailing source tags (e.g. "- NDTV Sports")
    for brand in COMPETITOR_BRANDS:
        text = re.sub(rf"\b{re.escape(brand)}\b", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s*[-|–—]\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_fresh_entry(entry, max_hours=24):
    """24 घंटे से पुरानी (बासी) खबरों को रोकता है"""
    t_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t_struct:
        return True
    try:
        pub_ts = time.mktime(t_struct)
        age_hours = (time.time() - pub_ts) / 3600.0
        return age_hours <= max_hours
    except Exception:
        return True

def is_duplicate(url_hash, title, state):
    if url_hash in state["hashes"]:
        return True
    clean_title = title.lower()
    for old_title in state["titles"]:
        if SequenceMatcher(None, clean_title, old_title).ratio() > 0.62:
            return True
    return False

def is_junk_news(title, summary):
    combined = f"{title} {summary}".lower()
    return any(junk in combined for junk in JUNK_KEYWORDS)

def score_entry(entry, index, trending_words_counter):
    title = strip_links_and_brands(entry.get("title", ""))
    summary = strip_links_and_brands(entry.get("summary", ""))
    if is_junk_news(title, summary) or not is_fresh_entry(entry, max_hours=24):
        return -999

    text = f"{title} {summary}".lower()
    score = max(0, 25 - index)

    for kw in PRIORITY_KEYWORDS:
        if kw in text:
            score += 12

    if any(k in text for k in ["india", "kohli", "rohit", "bumrah", "ipl", "bcci", "world cup", "champions trophy"]):
        score += 18

    # Cross-Feed Trending Boost: अगर कोई टॉपिक कई न्यूज़ फीड्स में एक साथ चल रहा है
    words = [w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in {"with", "from", "that", "this", "after", "over", "into", "against"}]
    for w in set(words):
        if trending_words_counter.get(w, 0) >= 2:
            score += trending_words_counter[w] * 4

    return score

def get_available_models():
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

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok and "edgesuite.net" not in resp.text.lower():
            soup = BeautifulSoup(resp.text, "html.parser")

            # सबसे पहले आर्टिकल की हाई-रेज़ोल्यूशन og:image लें
            for meta_prop in [("property", "og:image"), ("name", "twitter:image")]:
                tag = soup.find("meta", attrs={meta_prop[0]: meta_prop[1]})
                if tag and tag.get("content"):
                    image_url = tag["content"].strip()
                    break

            paragraphs = []
            for p in soup.find_all("p"):
                p_txt = strip_links_and_brands(p.get_text())
                if len(p_txt) > 50 and not any(bad in p_txt.lower() for bad in [
                    "cookies", "subscribe", "newsletter", "all rights reserved",
                    "click here", "read more", "follow us", "whatsapp", "telegram"
                ]):
                    paragraphs.append(p_txt)
            article_text = " ".join(paragraphs[:8])
    except Exception:
        pass

    if not image_url:
        if "media_content" in entry and entry.media_content:
            image_url = entry.media_content[0].get("url")
        elif "media_thumbnail" in entry and entry.media_thumbnail:
            image_url = entry.media_thumbnail[0].get("url")

    return image_url, strip_links_and_brands(article_text)

def download_real_hd_image(image_url, source_link=""):
    """केवल असली और साफ HD स्पोर्ट्स फोटो डाउनलोड करता है (छोटे या टूटे आइकन छोड़ देता है)"""
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
            if r.ok and len(r.content) > 8000:
                with Image.open(BytesIO(r.content)) as test_im:
                    w, h = test_im.size
                    if w >= 380 and h >= 220:
                        return r.content
        except Exception:
            continue
    return None

def watermark_image_in_ram(image_bytes):
    """फोटो के अंदर @SportsDuniyaOfficial का प्रीमियम ब्रांडिंग टैग लगाता है"""
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
            fill=(15, 20, 30, 210),
            outline=(255, 195, 0, 240),
            width=max(1, int(font_size * 0.08))
        )
        draw.text((box_x1 + pad_x, box_y1 + pad_y - bbox[1]), BRAND_TAG, font=font, fill=(255, 255, 255, 255))

        combined = Image.alpha_composite(img, overlay).convert("RGB")
        out_buffer = BytesIO()
        combined.save(out_buffer, format="JPEG", quality=93)
        out_buffer.seek(0)
        out_buffer.name = "sports_duniya.jpg"
        return out_buffer

def parse_ai_json_response(raw_text):
    try:
        cleaned = re.sub(r"```json|```", "", raw_text, flags=re.IGNORECASE).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            return json.loads(cleaned[start:end + 1])
    except Exception:
        pass
    return None

def build_structured_post(title, description, extra_text, models):
    """
    हमेशा 100% सटीक स्ट्रक्चर बनाता है:
    - ऊपर 2-लाइन बोल्ड हेडिंग (Line 1: Main Headline, Line 2: Sub-headline)
    - नीचे 60 शब्दों की Human-Written Inshorts Summary
    - 1-लाइन फैन कमेंट सवाल + SEO Hashtags
    """
    clean_title = strip_links_and_brands(title)
    clean_desc = strip_links_and_brands(description)
    clean_extra = strip_links_and_brands(extra_text)

    prompt = f"""
    You are the Chief Sports Editor of '{BRAND_TAG}', a top-tier Inshorts-style Telegram sports channel.
    Write a 100% human-written, engaging, SEO-optimized English sports news post.
    If the input details are short, use your deep sports knowledge to add accurate context (player role, tournament importance, match situation, or key statistics) so the reader gets the complete story right here without needing any external link.
    STRICT RULE: Never mention any other media name (like NDTV, ESPN, BBC, Sky, PTI) and never include any URLs or links.

    Return ONLY a valid JSON object with these exact 5 keys:
    {{
      "headline": "[Sport Emoji like 🏏/⚽/🥊/♟️/🏎️/🎾] + [Punchy main headline in 8 to 11 words with full searchable Player/Team names]",
      "subheadline": "⚡ [Sharp 2nd-line sub-headline highlighting the key stat, score, or impact in 8 to 12 words]",
      "summary": "[Crisp Inshorts-style paragraph of strictly 55 to 65 words. Start with the biggest impact in sentence 1, followed by complete match/player context in natural human journalism style]",
      "question": "💬 [1-line engaging fan debate question ending with 'Drop your thoughts in the comments!']",
      "hashtags": "[5 high-search organic SEO hashtags separated by spaces, e.g. #Cricket #RohitSharma #INDvsAUS #TeamIndia #SportsDuniya]"
    }}

    Input Headline: {clean_title}
    Input Summary: {clean_desc}
    Article Context: {clean_extra[:1500]}
    """

    parsed = None

    # 1. Try Groq AI
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
                        "messages": [
                            {"role": "system", "content": "You are an expert sports editor. Always respond with a valid JSON object."},
                            {"role": "user", "content": prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "max_tokens": 500,
                        "temperature": 0.45
                    },
                    timeout=18
                )
                if resp.ok:
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    parsed = parse_ai_json_response(raw)
                    if parsed and parsed.get("headline") and parsed.get("summary"):
                        print(f"[AI SUCCESS] Generated via Groq ({groq_model})")
                        break
            except Exception as e:
                print(f"[GROQ WARN] {groq_model}: {e}")

    # 2. Fallback to Gemini Flash Models
    if not parsed and GEMINI_API_KEY:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.45}
        }
        for model_name in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            try:
                resp = requests.post(url, json=payload, timeout=22)
                if resp.ok:
                    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    parsed = parse_ai_json_response(raw)
                    if parsed and parsed.get("headline") and parsed.get("summary"):
                        print(f"[AI SUCCESS] Generated via Gemini ({model_name})")
                        break
            except Exception:
                continue

    # 3. Clean Fallback if both APIs are temporarily busy
    if not parsed or not parsed.get("headline") or not parsed.get("summary"):
        combined = f"{clean_desc} {clean_extra}".strip()
        words = combined.split()
        if len(words) >= 20:
            body = " ".join(words[:60]) + ("." if not combined.endswith(".") else "")
        else:
            body = (
                f"{clean_title}. This major update has become a key talking point across the sports world "
                f"as fans and analysts closely follow the latest developments and upcoming fixtures."
            )
        parsed = {
            "headline": f"🔥 {clean_title}",
            "subheadline": "⚡ Key Developments & Match Impact Explained",
            "summary": body,
            "question": "💬 What's your take on this? Drop your thoughts in the comments!",
            "hashtags": "#SportsNews #Cricket #Football #TrendingSports #SportsDuniya"
        }

    h1 = strip_links_and_brands(str(parsed.get("headline", ""))).replace("*", "").replace("#", "")
    h2 = strip_links_and_brands(str(parsed.get("subheadline", ""))).replace("*", "").replace("#", "")
    if not h2.startswith("⚡"):
        h2 = f"⚡ {h2}"

    summary_body = strip_links_and_brands(str(parsed.get("summary", ""))).replace("*", "")
    question_line = strip_links_and_brands(str(parsed.get("question", "💬 What's your take on this? Comment below!"))).replace("*", "")
    if not question_line.startswith("💬"):
        question_line = f"💬 {question_line}"

    raw_tags = str(parsed.get("hashtags", "#Cricket #Football #SportsNews #SportsDuniya"))
    clean_tags = " ".join(re.findall(r"#\w+", raw_tags)) or "#Cricket #Football #SportsNews #SportsDuniya"
    if "#sportsduniya" not in clean_tags.lower():
        clean_tags += " #SportsDuniya"

    caption_html = (
        f"<b>{html.escape(h1)}</b>\n"
        f"<b>{html.escape(h2)}</b>\n\n"
        f"{html.escape(summary_body)}\n\n"
        f"<i>{html.escape(question_line)}</i>\n\n"
        f"{html.escape(clean_tags)}\n"
        f"⚡ <b>{BRAND_TAG}</b>"
    )
    return caption_html

def post_to_telegram(image_url, caption_html, source_link=""):
    img_bytes = download_real_hd_image(image_url, source_link)
    if not img_bytes:
        print("[IMAGE SKIP] No valid HD image found, trying next trending news...")
        return False

    branded_buffer = watermark_image_in_ram(img_bytes)

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": branded_buffer}
    # No inline_keyboard so Telegram's native Linked Group "Leave a comment" bar works!
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption_html[:1024],
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
    raw_entries = []
    word_counter = Counter()
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}

    for feed_url in RSS_FEEDS:
        try:
            r = requests.get(feed_url, headers=req_headers, timeout=8)
            if not r.ok:
                continue
            feed = feedparser.parse(r.content)
            for idx, entry in enumerate(feed.entries[:15]):
                link = entry.get("link", "")
                title = strip_links_and_brands(entry.get("title", ""))
                if not link or not title or len(title) < 15:
                    continue
                url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
                if is_duplicate(url_hash, title, state):
                    continue
                words = [w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in {"with", "from", "that", "this", "after", "over", "into", "against"}]
                word_counter.update(set(words))
                raw_entries.append((idx, entry, link, url_hash, title))
        except Exception as e:
            print(f"[RSS SKIP] {feed_url}: {e}")
            continue

    candidates = []
    for idx, entry, link, url_hash, title in raw_entries:
        score = score_entry(entry, idx, word_counter)
        if score < 0:
            continue
        candidates.append((score, entry, link, url_hash, title))

    candidates.sort(key=lambda x: x[0], reverse=True)
    print(f"[SCAN] Found {len(candidates)} fresh trending sports stories.")

    for score, entry, link, url_hash, title in candidates[:15]:
        image_url, extra_text = get_article_data(entry, link)
        if not image_url:
            continue
        try:
            print(f"[POSTING] Trending Score {score}: {title}")
            caption_html = build_structured_post(title, entry.get("summary", ""), extra_text, models)
            if post_to_telegram(image_url, caption_html, link):
                state["hashes"].append(url_hash)
                state["titles"].append(title.lower())
                state["last_post_ts"] = int(time.time())
                save_state(state, push_to_git=True)
                del caption_html, extra_text, image_url
                gc.collect()
                print("[SUCCESS] Posted with 2-Line Heading, Inshorts Summary & @SportsDuniyaOfficial tag!")
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
        f"💬 <b>Format:</b> 2-Line Heading + Inshorts + Native Comments ON 🟢\n\n"
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
                        data={"callback_query_id": cb_id, "text": "🚀 Generating & posting top trending news..."},
                        timeout=8
                    )
                    ok = execute_news_post(state, models, force=True)
                    send_or_edit_panel(chat_id, state, msg_id)
                    status_msg = "✅ Top trending news posted to channel!" if ok else "⚠️ No new trending news found right now."
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

    print(f"[BOT STARTED] Active Interval: {state.get('interval_mins', 60)} mins | Pro Inshorts Mode")
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
