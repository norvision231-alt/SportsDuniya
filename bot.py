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

# 👑 MASTER ADMIN & BRAND CONFIG
ADMIN_ID = int((os.getenv("ADMIN_ID", "7412791600") or "7412791600").strip())
BRAND_TAG = "@SportsDuniyaOfficial"
SUBSCRIPTION_STARS = 55  # ~₹99 in Telegram Stars (XTR)

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
    "https://www.theguardian.com/sport/rss",
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
    "bbc sport", "bbc", "reuters", "pti", "associated press", "ani", "cricbuzz", "the guardian",
    "times of india", "hindustan times", "indian express", "sportskeeda", "insidesport"
]

HASH_FILE = "posted_hashes.json"
git_lock = threading.Lock()
user_input_states = {}  # Tracks step-by-step inputs (e.g. setting channel or brand tag)

def get_ist_now():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_tz)

def is_master(uid):
    try:
        return int(uid) == ADMIN_ID
    except Exception:
        return False

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
    print(f"AI Engines Ready -> Groq: {'YES' if GROQ_API_KEY else 'NO'} | Gemini: {'YES' if GEMINI_API_KEY else 'NO'} | Master Admin: {ADMIN_ID}")

def sync_git_pull():
    os.system("git pull --rebase -X theirs origin main >/dev/null 2>&1 || true")

def _background_git_push():
    with git_lock:
        os.system('git config --global user.name "github-actions[bot]"')
        os.system('git config --global user.email "github-actions[bot]@users.noreply.github.com"')
        os.system("git add posted_hashes.json")
        os.system('git commit -m "Update bot state & client subscriptions" >/dev/null 2>&1 || true')
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
        "last_update_id": 0,
        "clients": {}  # user_id -> {name, paid, expires_at, channel_id, brand_tag, active}
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

def is_client_active_and_paid(client_obj):
    if not client_obj or not client_obj.get("paid"):
        return False
    exp = float(client_obj.get("expires_at", 0))
    return exp > time.time()

def strip_links_and_brands(text):
    """बाहरी लिंक्स, रीडायरेक्ट, एरर कोड और दूसरी मीडिया कंपनियों के नाम को पूरी तरह हटाता है"""
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"reference\s*#[\w\d.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"access denied.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\b[\w.-]+\.(edgesuite|net|com|org|in|co\.uk|tv|me|io)\S*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@(?!SportsDuniyaOfficial\b)\w+", "", text, flags=re.IGNORECASE)

    for brand in COMPETITOR_BRANDS:
        text = re.sub(rf"\b{re.escape(brand)}\b", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s*[-|–—]\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_fresh_entry(entry, max_hours=24):
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

    if any(k in text for k in ["india", "kohli", "rohit", "bumrah", "dhoni", "ipl", "bcci", "world cup", "csk", "rcb"]):
        score += 18

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

def extract_rss_image(entry):
    if "media_content" in entry and entry.media_content:
        for mc in entry.media_content:
            if mc.get("url"):
                return mc["url"]
    if "media_thumbnail" in entry and entry.media_thumbnail:
        for mt in entry.media_thumbnail:
            if mt.get("url"):
                return mt["url"]
    if "links" in entry:
        for lnk in entry.links:
            if "image" in str(lnk.get("type", "")) and lnk.get("href"):
                return lnk["href"]
    for field in ["storyimage", "image", "fullimage"]:
        if entry.get(field) and isinstance(entry.get(field), str):
            return entry.get(field)

    raw_html = entry.get("summary", "") or entry.get("description", "")
    if "content" in entry and entry.content:
        raw_html += " " + " ".join([c.get("value", "") for c in entry.content])
    if "<img" in raw_html:
        soup = BeautifulSoup(raw_html, "html.parser")
        img_tag = soup.find("img")
        if img_tag and img_tag.get("src") and img_tag["src"].startswith("http"):
            return img_tag["src"]
    return None

def get_article_data(entry, url):
    image_url = extract_rss_image(entry)
    article_text = ""

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = requests.get(url, headers=headers, timeout=9)
        if resp.ok and "edgesuite.net" not in resp.text.lower():
            soup = BeautifulSoup(resp.text, "html.parser")

            for meta_prop in [("property", "og:image"), ("name", "twitter:image")]:
                tag = soup.find("meta", attrs={meta_prop[0]: meta_prop[1]})
                if tag and tag.get("content") and tag["content"].startswith("http"):
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

    return image_url, strip_links_and_brands(article_text)

def search_bing_sports_image(query_title):
    clean_q = re.sub(r"[^\w\s]", " ", query_title)
    clean_q = " ".join(clean_q.split()[:9]) + " sports photo"
    search_url = f"https://www.bing.com/images/search?q={urllib.parse.quote(clean_q)}&qft=+filterui:imagesize-large"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(search_url, headers=headers, timeout=10)
        if r.ok:
            urls = re.findall(r"murl&quot;:&quot;(https?://[^&]+?\.(?:jpg|jpeg|png))", r.text, flags=re.IGNORECASE)
            if not urls:
                urls = re.findall(r'"murl":"(https?://[^"]+?\.(?:jpg|jpeg|png))"', r.text, flags=re.IGNORECASE)
            return urls[:6]
    except Exception:
        pass
    return []

def validate_and_normalize_image(raw_bytes):
    try:
        with Image.open(BytesIO(raw_bytes)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w < 220 or h < 140:
                return None
            if w < 800:
                new_w = 1080
                new_h = int(h * (1080 / float(w)))
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            out = BytesIO()
            im.save(out, format="JPEG", quality=93)
            return out.getvalue()
    except Exception:
        return None

def download_real_hd_image(image_url, title="", source_link=""):
    candidate_urls = []

    if image_url:
        upgraded = re.sub(r"/standard/\d+/", "/standard/1024/", image_url)
        clean_url = upgraded.split("?")[0] if "ndtvimg.com" in upgraded else upgraded
        encoded_clean = urllib.parse.quote(clean_url, safe="")
        encoded_full = urllib.parse.quote(upgraded, safe="")
        stripped_scheme = re.sub(r"^https?://", "", clean_url)

        candidate_urls.extend([
            f"https://wsrv.nl/?url={encoded_clean}&w=1200&output=jpg",
            f"https://wsrv.nl/?url={encoded_full}&w=1200&output=jpg",
            f"https://i0.wp.com/{stripped_scheme}",
            clean_url,
            upgraded
        ])

    if title:
        bing_urls = search_bing_sports_image(title)
        for b_url in bing_urls:
            enc_b = urllib.parse.quote(b_url, safe="")
            candidate_urls.append(b_url)
            candidate_urls.append(f"https://wsrv.nl/?url={enc_b}&w=1200&output=jpg")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": source_link if source_link else "https://www.google.com/"
    }

    for try_url in candidate_urls:
        try:
            r = requests.get(try_url, headers=headers, timeout=10)
            if r.ok and len(r.content) > 3500:
                valid_bytes = validate_and_normalize_image(r.content)
                if valid_bytes:
                    return valid_bytes
        except Exception:
            continue
    return None

def watermark_image_in_ram(image_bytes, custom_brand=None):
    """फोटो के अंदर @SportsDuniyaOfficial या पेड क्लाइंट का कस्टम ब्रांड टैग लगाता है"""
    tag_text = (custom_brand or BRAND_TAG).strip()
    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGBA")
        width, height = img.size

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(18, int(width * 0.028))
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

        bbox = draw.textbbox((0, 0), tag_text, font=font)
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
            fill=(15, 20, 30, 215),
            outline=(255, 195, 0, 240),
            width=max(1, int(font_size * 0.08))
        )
        draw.text((box_x1 + pad_x, box_y1 + pad_y - bbox[1]), tag_text, font=font, fill=(255, 255, 255, 255))

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

def generate_structured_post_parts(title, description, extra_text, models):
    """एक बार AI कॉल करके पोस्ट के 5 हिस्से बनाता है ताकि सभी चैनलों पर उनके अपने ब्रांड नाम से भेजा जा सके"""
    clean_title = strip_links_and_brands(title)
    clean_desc = strip_links_and_brands(description)
    clean_extra = strip_links_and_brands(extra_text)

    prompt = f"""
    You are the Chief Sports Editor of a top-tier Inshorts-style Telegram sports network.
    Write a 100% human-written, engaging, SEO-optimized English sports news post.
    If the input details are short, use your deep sports knowledge to add accurate context (player role, tournament importance, match situation, or key statistics) so the reader gets the complete story right here without needing any external link.
    STRICT RULE: Never mention any other media name (like NDTV, ESPN, BBC, Sky, PTI) and never include any URLs or links.

    Return ONLY a valid JSON object with these exact 5 keys:
    {{
      "headline": "[Sport Emoji like 🏏/⚽/🥊/♟️/🏎️/🎾] + [Punchy main headline in 8 to 11 words with full searchable Player/Team names]",
      "subheadline": "⚡ [Sharp 2nd-line sub-headline highlighting the key stat, score, or impact in 8 to 12 words]",
      "summary": "[Crisp Inshorts-style paragraph of strictly 55 to 65 words. Start with the biggest impact in sentence 1, followed by complete match/player context in natural human journalism style]",
      "question": "💬 [1-line engaging fan debate question ending with 'Drop your thoughts in the comments!']",
      "hashtags": "[4 high-search organic SEO hashtags separated by spaces, e.g. #Cricket #MSDhoni #IPL #TeamIndia]"
    }}

    Input Headline: {clean_title}
    Input Summary: {clean_desc}
    Article Context: {clean_extra[:1500]}
    """

    parsed = None

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
            "hashtags": "#SportsNews #Cricket #Football #TrendingSports"
        }

    h1 = strip_links_and_brands(str(parsed.get("headline", ""))).replace("*", "").replace("#", "")
    h2 = strip_links_and_brands(str(parsed.get("subheadline", ""))).replace("*", "").replace("#", "")
    if not h2.startswith("⚡"):
        h2 = f"⚡ {h2}"

    summary_body = strip_links_and_brands(str(parsed.get("summary", ""))).replace("*", "")
    question_line = strip_links_and_brands(str(parsed.get("question", "💬 What's your take on this? Comment below!"))).replace("*", "")
    if not question_line.startswith("💬"):
        question_line = f"💬 {question_line}"

    raw_tags = str(parsed.get("hashtags", "#Cricket #Football #SportsNews"))
    clean_tags = " ".join(re.findall(r"#\w+", raw_tags)) or "#Cricket #Football #SportsNews"

    return {
        "h1": h1,
        "h2": h2,
        "summary": summary_body,
        "question": question_line,
        "tags": clean_tags
    }

def format_caption_for_brand(post_parts, custom_brand=None):
    brand = (custom_brand or BRAND_TAG).strip()
    brand_hashtag = "#" + re.sub(r"[^a-zA-Z0-9]", "", brand)
    tags = post_parts["tags"]
    if brand_hashtag.lower() not in tags.lower() and len(brand_hashtag) > 2:
        tags = f"{tags} {brand_hashtag}"

    return (
        f"<b>{html.escape(post_parts['h1'])}</b>\n"
        f"<b>{html.escape(post_parts['h2'])}</b>\n\n"
        f"{html.escape(post_parts['summary'])}\n\n"
        f"<i>{html.escape(post_parts['question'])}</i>\n\n"
        f"{html.escape(tags)}\n"
        f"⚡ <b>{html.escape(brand)}</b>"
    )

def send_branded_photo_to_channel(target_channel_id, raw_img_bytes, post_parts, custom_brand=None):
    branded_buffer = watermark_image_in_ram(raw_img_bytes, custom_brand=custom_brand)
    caption_html = format_caption_for_brand(post_parts, custom_brand=custom_brand)

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": branded_buffer}
    data = {
        "chat_id": target_channel_id,
        "caption": caption_html[:1024],
        "parse_mode": "HTML"
    }

    try:
        res = requests.post(tg_url, data=data, files=files, timeout=20)
        ok = res.ok
        if not ok:
            print(f"Telegram API Error for {target_channel_id} ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Send Error for {target_channel_id}: {e}")
        ok = False

    branded_buffer.close()
    del branded_buffer
    gc.collect()
    return ok

def execute_news_post(state, models, force=False, single_client_channel=None, single_client_brand=None):
    ist_now = get_ist_now()
    interval_mins = int(state.get("interval_mins", 60))
    night_pause = state.get("night_pause", True)

    if not force and not single_client_channel:
        if night_pause and (0 <= ist_now.hour < 4):
            return False
        elapsed_mins = (int(time.time()) - state.get("last_post_ts", 0)) / 60
        if elapsed_mins < interval_mins:
            return False

    print(f"\n[POST TRIGGERED] IST: {ist_now.strftime('%I:%M %p')} | Interval: {interval_mins}m | Force: {force}")
    raw_entries = []
    seen_batch_titles = []
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
                if not single_client_channel and is_duplicate(url_hash, title, state):
                    continue
                if any(SequenceMatcher(None, title.lower(), st).ratio() > 0.65 for st in seen_batch_titles):
                    continue
                seen_batch_titles.append(title.lower())

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
    print(f"[SCAN] Found {len(candidates)} unique trending sports stories.")

    for score, entry, link, url_hash, title in candidates[:12]:
        try:
            image_url, extra_text = get_article_data(entry, link)
            img_bytes = download_real_hd_image(image_url, title=title, source_link=link)
            if not img_bytes:
                print(f"[IMAGE SKIP] Could not fetch image for: {title}")
                continue

            print(f"[POSTING] Trending Score {score}: {title}")
            post_parts = generate_structured_post_parts(title, entry.get("summary", ""), extra_text, models)

            # A. If testing for a single paid client channel
            if single_client_channel:
                ok = send_branded_photo_to_channel(single_client_channel, img_bytes, post_parts, custom_brand=single_client_brand)
                del img_bytes, post_parts
                gc.collect()
                return ok

            # B. Post to Master Admin Channel (@SportsDuniyaOfficial)
            master_ok = send_branded_photo_to_channel(TELEGRAM_CHANNEL_ID, img_bytes, post_parts, custom_brand=BRAND_TAG)

            # C. Broadcast to all Active Paid Client Channels with THEIR Custom Watermark & Brand Tag!
            clients_map = state.get("clients", {})
            for cid_uid, c_info in clients_map.items():
                if is_client_active_and_paid(c_info) and c_info.get("active", True) and c_info.get("channel_id"):
                    c_chan = c_info["channel_id"]
                    c_brand = c_info.get("brand_tag") or BRAND_TAG
                    send_branded_photo_to_channel(c_chan, img_bytes, post_parts, custom_brand=c_brand)
                    time.sleep(0.4)

            if master_ok:
                state["hashes"].append(url_hash)
                state["titles"].append(title.lower())
                state["last_post_ts"] = int(time.time())
                save_state(state, push_to_git=True)
                del post_parts, extra_text, img_bytes
                gc.collect()
                print("[SUCCESS] Posted to Master Channel & all active Paid Client Channels!")
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

# =========================================================
# 👑 MASTER ADMIN PANEL vs 💎 PAID CLIENT SAAS PANEL
# =========================================================
def send_stars_subscription_invoice(chat_id, user_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendInvoice"
    prices = [{"label": "₹99 Pro Custom Branding Plan (30 Days)", "amount": SUBSCRIPTION_STARS}]
    payload = {
        "chat_id": chat_id,
        "title": "Sports Auto-News Bot (Pro Custom Branding)",
        "description": "Activate 24/7 Automatic Trending Sports News in your channel with YOUR Custom Image Watermark & Brand Tag (₹99 / Stars Plan).",
        "payload": f"sports_saas_{user_id}",
        "provider_token": "",
        "currency": "XTR",
        "prices": json.dumps(prices)
    }
    requests.post(url, data=payload, timeout=10)

def build_master_admin_panel(state):
    mins = int(state.get("interval_mins", 60))
    night = "ON (12 AM - 4 AM)" if state.get("night_pause", True) else "OFF (24x7 Active)"
    last_ts = state.get("last_post_ts", 0)
    elapsed = int((time.time() - last_ts) / 60) if last_ts else 0
    remaining = max(0, mins - elapsed) if last_ts else 0

    clients = state.get("clients", {})
    paid_count = sum(1 for c in clients.values() if is_client_active_and_paid(c))

    text = (
        f"👑 <b>Sports Duniya Master Admin Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ <b>Current Interval:</b> {format_interval(mins)}\n"
        f"🌙 <b>Night Pause:</b> {night}\n"
        f"⏳ <b>Next Auto Post In:</b> ~{remaining} mins\n"
        f"💎 <b>Paid SaaS Clients:</b> {paid_count} Active (Total Users: {len(clients)})\n"
        f"💰 <b>Client Price:</b> ₹99 ({SUBSCRIPTION_STARS} Telegram Stars)\n\n"
        f"🛠 <b>Admin Commands:</b>\n"
        f"• <code>/set 30m</code> or <code>/set 2h</code> — कस्टम टाइम सेट करें\n"
        f"• <code>/grant USER_ID</code> — किसी यूज़र को 30 दिन का फ्री/UPI एक्सेस दें\n"
        f"• <code>/revoke USER_ID</code> — किसी यूज़र का एक्सेस बंद करें"
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
                {"text": "🚀 Post Now (All Channels)", "callback_data": "post_now"},
                {"text": f"🌙 Night: {'ON' if state.get('night_pause', True) else 'OFF'}", "callback_data": "toggle_night"}
            ],
            [
                {"text": f"👥 Manage Paid Clients ({paid_count})", "callback_data": "adm_clients"}
            ]
        ]
    }
    return text, keyboard

def build_client_saas_panel(user_id, user_name, state):
    uid = str(user_id)
    clients = state.get("clients", {})
    if uid not in clients:
        clients[uid] = {
            "name": user_name,
            "paid": False,
            "expires_at": 0,
            "channel_id": "",
            "brand_tag": "",
            "active": True
        }
    c = clients[uid]
    is_paid = is_client_active_and_paid(c)
    days_left = max(0, int((float(c.get("expires_at", 0)) - time.time()) / 86400)) if is_paid else 0
    chan = c.get("channel_id") or "Not Set ❌"
    brand = c.get("brand_tag") or "Not Set ❌"
    status_line = f"🟢 <b>PRO ACTIVE ({days_left} Days Left)</b>" if is_paid else "🔒 <b>LOCKED (Pay ₹99 Stars to Activate)</b>"

    text = (
        f"🚀 <b>Sports Auto-News Bot (Custom Branding SaaS)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {html.escape(user_name)} (ID: <code>{uid}</code>)\n"
        f"💳 <b>Plan Status:</b> {status_line}\n"
        f"📢 <b>Your Channel:</b> <code>{html.escape(str(chan))}</code>\n"
        f"🏷️ <b>Your Brand Watermark:</b> <code>{html.escape(str(brand))}</code>\n\n"
        f"✨ <b>Features You Get (₹99 / Month):</b>\n"
        f"• 24/7 Automatic Trending Sports News (Inshorts Style)\n"
        f"• <b>YOUR Custom Brand Watermark</b> on every HD Photo\n"
        f"• <b>YOUR Custom Tag</b> at the bottom of every post\n"
        f"• 100% Clean SEO English Posts with Zero External Links!"
    )

    if not is_paid:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"⭐ Pay ₹99 ({SUBSCRIPTION_STARS} Stars) — Instant Unlock", "callback_data": "client_buy"}],
                [{"text": "🔄 Refresh Status", "callback_data": "client_home"}]
            ]
        }
    else:
        tog_label = "⏸️ Pause Auto-Posting" if c.get("active", True) else "▶️ Resume Auto-Posting"
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📢 Set My Channel", "callback_data": "client_set_chan"},
                    {"text": "🏷️ Set My Brand Tag", "callback_data": "client_set_brand"}
                ],
                [
                    {"text": "🧪 Send Test Post Now", "callback_data": "client_test_post"},
                    {"text": tog_label, "callback_data": "client_toggle"}
                ],
                [
                    {"text": f"⭐ Extend +30 Days (₹99 / {SUBSCRIPTION_STARS} Stars)", "callback_data": "client_buy"}
                ]
            ]
        }
    return text, keyboard

def send_or_edit_panel(chat_id, user_id, user_name, state, message_id=None):
    if is_master(user_id):
        text, keyboard = build_master_admin_panel(state)
    else:
        text, keyboard = build_client_saas_panel(user_id, user_name, state)

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

            # 1. Handle Telegram Stars Pre-Checkout Query (Must answer within 10s)
            if "pre_checkout_query" in upd:
                pcq = upd["pre_checkout_query"]
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerPreCheckoutQuery",
                    data={"pre_checkout_query_id": pcq["id"], "ok": True},
                    timeout=8
                )
                continue

            # 2. Handle Button Clicks
            if "callback_query" in upd:
                cb = upd["callback_query"]
                data = cb.get("data", "")
                chat_id = cb["message"]["chat"]["id"]
                msg_id = cb["message"]["message_id"]
                uid = cb["from"]["id"]
                uname = cb["from"].get("first_name", "User")
                cb_id = cb["id"]

                # --- CLIENT SAAS BUTTONS ---
                if data == "client_home":
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id}, timeout=5)
                    send_or_edit_panel(chat_id, uid, uname, state, msg_id)
                    continue

                if data == "client_buy":
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "💎 Sending ₹99 Telegram Stars Invoice..."},
                        timeout=5
                    )
                    send_stars_subscription_invoice(chat_id, uid)
                    continue

                if data in ["client_set_chan", "client_set_brand", "client_test_post", "client_toggle"]:
                    c_obj = state.get("clients", {}).get(str(uid))
                    if not is_client_active_and_paid(c_obj) and not is_master(uid):
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            data={"callback_query_id": cb_id, "text": "🔒 कृपया पहले ₹99 Stars पेमेंट करके अपना अकाउंट एक्टिवेट करें!", "show_alert": True},
                            timeout=6
                        )
                        continue

                    if data == "client_set_chan":
                        user_input_states[str(uid)] = "SET_CHAN"
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            data={
                                "chat_id": chat_id,
                                "text": "📢 <b>Step 1:</b> सबसे पहले इस बॉट को अपने Telegram Channel में <b>Admin (Post Messages)</b> बनाएं।\n\n<b>Step 2:</b> अब अपने चैनल का <code>@username</code> (जैसे <code>@MySportsChannel</code>) या Channel ID (<code>-100...</code>) नीचे लिखकर भेजें (रद्द करने के लिए /cancel लिखें):",
                                "parse_mode": "HTML"
                            },
                            timeout=8
                        )
                    elif data == "client_set_brand":
                        user_input_states[str(uid)] = "SET_BRAND"
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            data={
                                "chat_id": chat_id,
                                "text": "🏷️ <b>अपना कस्टम ब्रांड नाम या टैग लिखकर भेजें:</b>\n(जैसे: <code>@MyCricketOfficial</code> या <code>Cricket Hub</code>)\n\nयही नाम आपकी हर फोटो के ऊपर Watermark में और पोस्ट के नीचे छपेगा:",
                                "parse_mode": "HTML"
                            },
                            timeout=8
                        )
                    elif data == "client_toggle":
                        c_obj["active"] = not c_obj.get("active", True)
                        save_state(state, push_to_git=True)
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id, "text": "✅ Status Updated!"}, timeout=5)
                        send_or_edit_panel(chat_id, uid, uname, state, msg_id)
                    elif data == "client_test_post":
                        if not c_obj.get("channel_id"):
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                data={"callback_query_id": cb_id, "text": "⚠️ कृपया पहले '📢 Set My Channel' दबाकर अपना चैनल सेट करें!", "show_alert": True},
                                timeout=6
                            )
                            continue
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            data={"callback_query_id": cb_id, "text": "🚀 आपके कस्टम वॉटरमार्क के साथ टेस्ट पोस्ट भेजी जा रही है..."},
                            timeout=6
                        )
                        ok = execute_news_post(
                            state,
                            models,
                            force=True,
                            single_client_channel=c_obj["channel_id"],
                            single_client_brand=c_obj.get("brand_tag") or BRAND_TAG
                        )
                        msg_txt = "✅ आपके चैनल पर आपके कस्टम ब्रांड वॉटरमार्क के साथ पोस्ट भेज दी गई है!" if ok else "❌ पोस्ट नहीं जा सकी! कृपया चेक करें कि बॉट आपके चैनल में Admin है।"
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": msg_txt}, timeout=8)
                    continue

                # --- MASTER ADMIN ONLY BUTTONS ---
                if not is_master(uid):
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "🔒 यह बटन केवल Master Admin के लिए है!", "show_alert": True},
                        timeout=5
                    )
                    continue

                if data == "adm_clients":
                    clients = state.get("clients", {})
                    lines = ["👥 <b>Registered & Paid Clients List:</b>\n"]
                    for k_uid, c_info in list(clients.items())[-20:]:
                        paid_badge = "🟢 PAID" if is_client_active_and_paid(c_info) else "🔴 UNPAID"
                        lines.append(
                            f"• <b>{html.escape(c_info.get('name', 'User'))}</b> (<code>{k_uid}</code>) — {paid_badge}\n"
                            f"  📢 Chan: <code>{html.escape(str(c_info.get('channel_id') or 'None'))}</code> | 🏷️ Brand: <code>{html.escape(str(c_info.get('brand_tag') or 'None'))}</code>"
                        )
                    if len(lines) == 1:
                        lines.append("अभी कोई क्लाइंट रजिस्टर नहीं हुआ है।")
                    lines.append("\n💡 <i>किसी को मैन्युअल 30 दिन अनलॉक करने के लिए</i> <code>/grant USER_ID</code> <i>लिखें।</i>")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id}, timeout=5)
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"},
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
                    send_or_edit_panel(chat_id, uid, uname, state, msg_id)
                    save_state(state, push_to_git=True)

                elif data == "toggle_night":
                    state["night_pause"] = not state.get("night_pause", True)
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "✅ Night mode updated!"},
                        timeout=8
                    )
                    send_or_edit_panel(chat_id, uid, uname, state, msg_id)
                    save_state(state, push_to_git=True)

                elif data == "post_now":
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb_id, "text": "🚀 Generating & posting top trending news..."},
                        timeout=8
                    )
                    ok = execute_news_post(state, models, force=True)
                    send_or_edit_panel(chat_id, uid, uname, state, msg_id)
                    status_msg = "✅ Top trending news posted to Master & Client channels!" if ok else "⚠️ No new trending news found right now."
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": chat_id, "text": status_msg},
                        timeout=8
                    )

            # 3. Handle Messages & Successful Payments
            elif "message" in upd:
                msg = upd["message"]
                if msg["chat"].get("type", "private") != "private":
                    continue
                chat_id = msg["chat"]["id"]
                uid = msg.get("from", {}).get("id", chat_id)
                uname = msg.get("from", {}).get("first_name", "User")

                # A. Handle Successful ₹99 Stars Payment!
                if "successful_payment" in msg:
                    uid_str = str(uid)
                    if "clients" not in state:
                        state["clients"] = {}
                    if uid_str not in state["clients"]:
                        state["clients"][uid_str] = {"name": uname, "channel_id": "", "brand_tag": "", "active": True}

                    curr_exp = float(state["clients"][uid_str].get("expires_at", 0))
                    base_ts = max(time.time(), curr_exp)
                    state["clients"][uid_str]["paid"] = True
                    state["clients"][uid_str]["expires_at"] = base_ts + (30 * 86400)
                    save_state(state, push_to_git=True)

                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={
                            "chat_id": chat_id,
                            "text": "🎉 <b>Payment Successful (₹99 Stars Plan Activated)!</b>\n\nअब आप नीचे दिए गए पैनल से अपना <b>Channel</b> और <b>Custom Brand Watermark</b> सेट कर सकते हैं:",
                            "parse_mode": "HTML"
                        },
                        timeout=8
                    )
                    send_or_edit_panel(chat_id, uid, uname, state)

                    # Notify Master Admin
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={
                            "chat_id": ADMIN_ID,
                            "text": f"💰 <b>New ₹99 Stars Subscription Received!</b>\n👤 User: {html.escape(uname)} (<code>{uid}</code>)\n✅ 30-Day Custom Branding Unlocked!",
                            "parse_mode": "HTML"
                        },
                        timeout=8
                    )
                    continue

                if "text" not in msg:
                    continue

                raw_text = msg["text"].strip()
                low_text = raw_text.lower()

                if low_text == "/cancel":
                    user_input_states.pop(str(uid), None)
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": "✅ रद्द किया गया।"}, timeout=6)
                    send_or_edit_panel(chat_id, uid, uname, state)
                    continue

                # B. Handle Step-by-Step Client Input (Channel ID or Brand Tag)
                pending_action = user_input_states.get(str(uid))
                if pending_action and not low_text.startswith("/"):
                    user_input_states.pop(str(uid), None)
                    if "clients" not in state:
                        state["clients"] = {}
                    if str(uid) not in state["clients"]:
                        state["clients"][str(uid)] = {"name": uname, "paid": is_master(uid), "expires_at": time.time() + 30*86400, "active": True}

                    if pending_action == "SET_CHAN":
                        ch_val = raw_text
                        if "t.me/" in ch_val:
                            ch_val = "@" + ch_val.split("t.me/")[-1].strip("/")
                        elif not ch_val.startswith("@") and not ch_val.startswith("-100"):
                            ch_val = "@" + ch_val
                        state["clients"][str(uid)]["channel_id"] = ch_val
                        save_state(state, push_to_git=True)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            data={"chat_id": chat_id, "text": f"✅ आपका चैनल <code>{html.escape(ch_val)}</code> सेव हो गया है!", "parse_mode": "HTML"},
                            timeout=8
                        )
                        send_or_edit_panel(chat_id, uid, uname, state)
                        continue

                    elif pending_action == "SET_BRAND":
                        b_val = raw_text[:28].strip()
                        state["clients"][str(uid)]["brand_tag"] = b_val
                        save_state(state, push_to_git=True)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            data={"chat_id": chat_id, "text": f"✅ आपकी कस्टम ब्रांडिंग <b>{html.escape(b_val)}</b> सेट हो गई है! अब हर फोटो और पोस्ट पर यही नाम दिखेगा।", "parse_mode": "HTML"},
                            timeout=8
                        )
                        send_or_edit_panel(chat_id, uid, uname, state)
                        continue

                # C. Standard Commands
                if low_text in ["/start", "/panel", "/settings", "/status", "panel", "menu"]:
                    send_or_edit_panel(chat_id, uid, uname, state)

                elif is_master(uid) and low_text.startswith("/grant"):
                    parts = raw_text.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        target_uid = parts[1]
                        if "clients" not in state:
                            state["clients"] = {}
                        if target_uid not in state["clients"]:
                            state["clients"][target_uid] = {"name": "VIP Client", "channel_id": "", "brand_tag": "", "active": True}
                        state["clients"][target_uid]["paid"] = True
                        state["clients"][target_uid]["expires_at"] = time.time() + (30 * 86400)
                        save_state(state, push_to_git=True)
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": f"✅ User {target_uid} को 30 दिन का Pro Custom Branding एक्सेस दे दिया गया है!"}, timeout=8)
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": target_uid, "text": "🎉 Master Admin ने आपका 30-Day Pro Custom Branding प्लान एक्टिवेट कर दिया है! /start दबाकर अपनी ब्रांडिंग सेट करें।"}, timeout=8)
                    else:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": "Usage: /grant USER_ID"}, timeout=8)

                elif is_master(uid) and low_text.startswith("/revoke"):
                    parts = raw_text.split()
                    if len(parts) >= 2 and parts[1] in state.get("clients", {}):
                        target_uid = parts[1]
                        state["clients"][target_uid]["paid"] = False
                        state["clients"][target_uid]["expires_at"] = 0
                        save_state(state, push_to_git=True)
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": f"🚫 User {target_uid} का एक्सेस बंद कर दिया गया है।"}, timeout=8)

                elif is_master(uid) and low_text.startswith("/set"):
                    m = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?", low_text)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "m"
                        mins = val * 60 if unit.startswith("h") else val
                        mins = max(5, min(1440, mins))
                        state["interval_mins"] = mins
                        send_or_edit_panel(chat_id, uid, uname, state)
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

    print(f"[BOT STARTED] Active Interval: {state.get('interval_mins', 60)} mins | Pro Multi-Tenant SaaS Mode")
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
