import os
import re
import json
import logging
import random
import datetime
import urllib.parse
from urllib.parse import urlparse, urlunparse
import requests
import feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
AI_API_KEY = os.environ.get("AI_API_KEY")

HISTORY_FILE = "history.json"
DIGEST_STATE_FILE = "digest_state.txt"

# === ИСТОЧНИКИ ===

FEEDS_MUSIC = [
    "https://the-flow.ru/rss",
    "https://pitchfork.com/rss/reviews/albums/",
    "https://ra.co/xml/news",
    "https://mixmag.net/rss.xml",
    "https://djmag.com/rss.xml",
    "https://hiphopdx.com/rss"
]

FEEDS_TECH = [
    "https://samesound.ru/feed",
    "https://cdm.link/feed/",
    "https://www.musicradar.com/rss"
]

KEYWORDS_MUSIC = [
    'релиз', 'альбом', 'трек', 'album', 'track', 'ep', 'клип', 'video',
    'хип-хоп', 'hip hop', 'рэп', 'rap', 'trap', 'drill', 'электроника', 'electronic',
    'techno', 'house', 'rave', 'премьера', 'новинка', 'сингл', 'single', 'слушать', 'review',
    'плейлист', 'playlist', 'подборка', 'микстейп', 'mixtape'
]

KEYWORDS_TECH = [
    'vst', 'plugin', 'плагин', 'ableton', 'fl studio', 'logic', 'cubase', 'reaper', 'daw',
    'synth', 'синтезатор', 'драм-машина', 'сэмпл', 'sample', 'midi', 'миди',
    'update', 'сведение', 'мастеринг', 'mixing', 'битмейкинг', 'sound design', 'железо', 'hardware'
]

# === УТИЛИТЫ ДЛЯ БОРЬБЫ С ДУБЛЯМИ И ХРАНЕНИЯ ===

def clean_url(url):
    parsed = urlparse(url)
    clean_query = '&'.join([q for q in parsed.query.split('&') if not q.startswith('utm_')])
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-200:], f, ensure_ascii=False, indent=2)

def is_digest_sent_today():
    today_str = datetime.date.today().isoformat()
    if os.path.exists(DIGEST_STATE_FILE):
        with open(DIGEST_STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == today_str
    return False

def mark_digest_sent():
    today_str = datetime.date.today().isoformat()
    with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
        f.write(today_str)

def clean_html_for_telegram(text):
    text = re.sub(r'</?(p|div|section|article|header|footer|html|body)[^>]*>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?(h1|h2|h3|h4|h5|h6)[^>]*>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text

# === ИЗВЛЕЧЕНИЕ ИЗОБРАЖЕНИЙ (С ПРОВЕРКОЙ WEB-СТРАНИЦЫ) ===

def extract_image_url(entry):
    """Сначала пробуем достать картинку из RSS-данных"""
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url')
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                return enc.get('href')

    html_content = ""
    if 'content' in entry:
        for c in entry.content:
            html_content += c.value
    elif 'description' in entry:
        html_content = entry.description

    if html_content:
        soup = BeautifulSoup(html_content, 'html.parser')
        img = soup.find('img')
        if img and img.get('src') and img['src'].startswith('http'):
            return img['src']
    return None

def fetch_og_image_fallback(article_url):
    """Если в RSS картинки нет, вытаскиваем оригинальную обложку (og:image) прямо со страницы"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        res = requests.get(article_url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content') and og_img['content'].startswith('http'):
                return og_img['content']
            tw_img = soup.find('meta', attrs={'name': 'twitter:image'})
            if tw_img and tw_img.get('content') and tw_img['content'].startswith('http'):
                return tw_img['content']
    except Exception as e:
        logging.warning(f"Не удалось спарсить og:image со страницы: {e}")
    return None

# === УМНЫЙ ПОИСК СТРИМИНГОВ ЧЕРЕЗ SONGLINK / ODESLI ===

def find_streaming_links(search_query, raw_html=""):
    headers = {"User-Agent": "SpeedOfSoundBot/1.0"}
    streaming_url = None

    if raw_html:
        patterns = [
            r'https?://open\.spotify\.com/(?:track|album)/[a-zA-Z0-9]+',
            r'https?://music\.apple\.com/[a-z]{2}/album/[^"\'\s>]+',
            r'https?://(?:www\.)?youtube\.com/watch\?v=[a-zA-Z0-9_-]+',
            r'https?://youtu\.be/[a-zA-Z0-9_-]+',
            r'https?://soundcloud\.com/[^"\'\s>]+'
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_html)
            if match:
                streaming_url = match.group(0)
                logging.info(f"Найдена прямая ссылка в статье: {streaming_url}")
                break

    if not streaming_url and search_query:
        try:
            itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(search_query)}&media=music&limit=1"
            res = requests.get(itunes_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('resultCount', 0) > 0:
                    item = data['results'][0]
                    streaming_url = item.get('trackViewUrl') or item.get('collectionViewUrl')
                    logging.info(f"Найдена ссылка через iTunes API: {streaming_url}")
        except Exception as e:
            logging.warning(f"Ошибка iTunes API: {e}")

    if streaming_url:
        try:
            odesli_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(streaming_url)}&userCountry=RU"
            res = requests.get(odesli_url, headers=headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                platforms = data.get('linksByPlatform', {})
                return {
                    "page_url": data.get('pageUrl'),
                    "yandex": platforms.get('yandex', {}).get('url'),
                    "spotify": platforms.get('spotify', {}).get('url')
                }
        except Exception as e:
            logging.warning(f"Ошибка Odesli API: {e}")

    if search_query:
        q_enc = urllib.parse.quote(search_query)
        return {
            "page_url": None,
            "yandex": f"https://music.yandex.ru/search?text={q_enc}",
            "spotify": f"https://open.spotify.com/search/{q_enc}"
        }

    return None

# === КЛАВИАТУРЫ ДЛЯ TELEGRAM ===

def build_music_keyboard(links, news_link):
    keyboard = []
    if links:
        if links.get("page_url"):
            keyboard.append([{"text": "🎧 Слушать релиз (все площадки)", "url": links["page_url"]}])
        service_row = []
        if links.get("yandex"):
            service_row.append({"text": "🔴 Яндекс Музыка", "url": links["yandex"]})
        if links.get("spotify"):
            service_row.append({"text": "🟢 Spotify", "url": links["spotify"]})
        if service_row:
            keyboard.append(service_row)

    if news_link:
        keyboard.append([{"text": "🔗 Читать первоисточник", "url": news_link}])
    return {"inline_keyboard": keyboard} if keyboard else None

def build_default_keyboard(news_link):
    if not news_link:
        return None
    return {"inline_keyboard": [[{"text": "🔗 Читать первоисточник", "url": news_link}]]}

# === КАСКАД МОДЕЛЕЙ GEMINI ===

def generate_text_with_fallback(prompt):
    genai.configure(api_key=AI_API_KEY)
    models_to_try = [
        'gemini-3.8-flash',
        'gemini-3.7-flash',
        'gemini-3.6-flash',
        'gemini-2.5-flash',
        'gemini-2.0-flash',
        'gemini-1.5-pro',
        'gemini-1.5-flash'
    ]

    for model_name in models_to_try:
        try:
            logging.info(f"Пробую модель {model_name}...")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            err = str(e).lower()
            if '429' in err or 'quota' in err or 'exhausted' in err:
                logging.warning(f"[{model_name}] Лимит исчерпан. Перехожу дальше...")
            else:
                logging.warning(f"[{model_name}] Ошибка: {e}. Перехожу дальше...")

    raise RuntimeError("Все доступные модели Gemini вернули ошибку.")

# === ГЕНЕРАЦИЯ ПОСТОВ С УМНОЙ ЛОГИКОЙ ===

def generate_music_post(news_item):
    prompt = f"""
Ты — музыкальный редактор и журналист Telegram-канала "Speed of Sound" (@speed_sound).
Канал читают как любители актуальной музыки, так и продюсеры.
Твоя цель — написать емкий, стильный и логичный пост без дешевого кликбейта и пафоса.

ОРИГИНАЛЬНАЯ НОВОСТЬ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

СТРОГИЕ ПРАВИЛА:
1. Заголовок: Сочный, журналистский, отражающий суть, в теге <b>...</b>.
2. КАРТОЧКА РЕЛИЗА (СТРОГО ПО УСЛОВИЮ):
   - Если новость посвящена КОНКРЕТНОМУ треку, синглу, альбому, клипу или плейлисту — добавь под заголовком строчку:
     🎧 <b>Жанр:</b> [Жанр] | <b>Вайб:</b> [2-3 слова о настроении]
   - Если это новость индустрии, скандал, закон, нейросети (как Suno/Udio), фестиваль или конфликт артистов — КАРТОЧКУ ВАЙБА НЕ ПИШИ ВООБЩЕ!
3. Первый абзац: Факты без воды. Что произошло / кто что выпустил / в чем суть инфоповода.
4. Второй абзац (Добавочная ценность): Трезвый анализ. 
   - Запрещено использовать клише: "главный звоночек года", "переворот в игре", "битва титанов", "навсегда изменит".
   - Пиши логично: почему это интересно слушателю, какие юридические/индустриальные последствия это несет или какие фишки звука/продакшена стоит подметить в релизе.
5. Объем текста: до 650 символов!
6. Хештеги в конце: 2-3 релевантных (#релиз #хипхоп #электроника #новости #speedofsound).
7. СЛУЖЕБНАЯ СТРОКА В САМОМ КОНЦЕ:
   - Если это релиз/плейлист: напиши "SEARCH: Артист - Название" (только имя и трек).
   - Если это общая новость индустрии без конкретного трека: напиши "SEARCH: NONE".

Напиши пост:
"""
    raw_text = generate_text_with_fallback(prompt)
    
    search_query = ""
    cleaned_lines = []
    for line in raw_text.strip().split('\n'):
        if line.strip().startswith('SEARCH:'):
            val = line.replace('SEARCH:', '').strip()
            if val != "NONE" and len(val) > 2:
                search_query = val
        else:
            cleaned_lines.append(line)
            
    final_text = clean_html_for_telegram('\n'.join(cleaned_lines))
    return final_text, search_query

def generate_tech_post(news_item):
    prompt = f"""
Ты — куратор Telegram-канала "Speed of Sound" (@speed_sound) и саунд-продюсер.
Перед тобой новость про студийный софт, плагин, девайс или инструмент.

ОРИГИНАЛ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА:
1. Заголовок: Прикладной и конкретный, в <b>...</b>. Без клише.
2. Первый абзац: Что за инструмент/плагин и для чего он нужен.
3. Второй абзац: Практическая польза. Как это помогает экономить время или улучшить микс. Пиши простым языком для продюсеров и музыкантов.
4. Объем текста: до 550 символов!
5. Хештеги: #продакшен #vst #ableton #plugins #speedofsound
6. Только теги <b> и <i>.

Напиши пост:
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

def gather_weekly_context():
    weekly_titles = []
    for feed_url in FEEDS_MUSIC + FEEDS_TECH[:1]:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:8]:
                weekly_titles.append(f"- {entry.title} ({urlparse(entry.link).netloc})")
        except Exception:
            continue
    return "\n".join(weekly_titles)

def generate_friday_digest():
    context = gather_weekly_context()
    prompt = f"""
Ты — музыкальный редактор "Speed of Sound". Пятница — день главных музыкальных релизов!
Вот список новостей и премьер недели:
{context}

ЗАДАЧА:
Собери "🔥 Пятничный дайджест: 7-8 главных релизов недели".
Фокус: зарубежный и русскоязычный рэп/хип-хоп, клубная и атмосферная электроника, громкие альбомы.
- Пронумерованный список от 1 до 8.
- Артист — Название: в 1-2 емких предложениях опиши, почему релиз стоит заценить.
- Без пафосных штампов, пиши со вкусом и по делу.
- В конце каждого трека напиши: "🎧 Слушать на площадках".
- Только HTML <b>. Лимит 2200 знаков.
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

# === ОТПРАВКА В TELEGRAM ===

def send_to_telegram(text, image_url=None, reply_markup=None, target_chat_id=CHANNEL_ID):
    url_base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    payload = {"chat_id": target_chat_id, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    # Отправка с фото
    if image_url and len(text) <= 1024:
        payload["photo"] = image_url
        payload["caption"] = text
        res = requests.post(url_base + "sendPhoto", json=payload)
        if res.status_code == 200:
            logging.info("Пост с изображением опубликован.")
            return True
        logging.warning(f"Ошибка фото ({res.text}). Пробую текстом...")

    # Отправка текстом
    payload.pop("photo", None)
    payload.pop("caption", None)
    payload["text"] = text
    payload["disable_web_page_preview"] = True
    
    res = requests.post(url_base + "sendMessage", json=payload)
    if res.status_code == 200:
        logging.info("Текстовый пост опубликован.")
        return True
    
    logging.error(f"Ошибка TG API: {res.text}")
    return False

# === СБОР КАНДИДАТОВ ИЗ ЛЕНТ ===

def collect_from_feeds(feed_urls, target_keywords, history):
    candidates = []
    shuffled_feeds = feed_urls.copy()
    random.shuffle(shuffled_feeds)
    
    for feed_url in shuffled_feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                link = clean_url(entry.link)
                if link in history:
                    continue
                
                title = entry.title
                summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
                clean_summary = BeautifulSoup(summary, "html.parser").get_text()
                
                text_to_check = (title + " " + clean_summary).lower()
                
                if any(kw in text_to_check for kw in target_keywords):
                    raw_html = ""
                    if 'content' in entry:
                        for c in entry.content:
                            raw_html += c.value
                    raw_html += (summary or '')

                    # Извлекаем картинку из RSS
                    img_url = extract_image_url(entry)

                    candidates.append({
                        "title": title,
                        "summary": clean_summary[:800],
                        "raw_html": raw_html,
                        "link": link,
                        "image_url": img_url,
                        "domain": urlparse(link).netloc
                    })
                    break
        except Exception as e:
            logging.error(f"Ошибка чтения {feed_url}: {e}")
            
    return candidates

# === ОСНОВНОЙ ЦИКЛ ===

def main():
    if not os.path.exists(DIGEST_STATE_FILE):
        with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
            f.write("")

    if not all([BOT_TOKEN, CHANNEL_ID, AI_API_KEY]):
        logging.error("Отсутствуют токены в Secrets!")
        return

    now = datetime.datetime.now()
    is_friday_morning = (now.weekday() == 4) and (8 <= now.hour < 12)

    # 1. ПЯТНИЧНЫЙ ДАЙДЖЕСТ
    if is_friday_morning:
        if not ADMIN_ID:
            logging.warning("Пятница, но ADMIN_ID не задан!")
        elif not is_digest_sent_today():
            logging.info("Пятница: отправка дайджеста в ЛС...")
            try:
                digest = generate_friday_digest()
                notice = (
                    "<b>🔔 ПЯТНИЧНЫЙ ДАЙДЖЕСТ (ЧЕРНОВИК НА СОГЛАСОВАНИЕ)</b>\n\n"
                    f"{digest}\n\n"
                    "<i>Отредактируй и перешли в канал, если всё Ок!</i>"
                )
                success = send_to_telegram(notice, target_chat_id=ADMIN_ID)
                if success:
                    mark_digest_sent()
                    logging.info("Дайджест доставлен админу.")
            except Exception as e:
                logging.error(f"Ошибка дайджеста: {e}")
            return
        else:
            logging.info("Дайджест уже отправлялся сегодня.")

    # 2. РЕГУЛЯРНЫЙ ПОСТИНГ: 70% МУЗЫКА, 30% ТЕХНИКА
    history = load_history()
    pick_music = random.random() < 0.70
    primary_cat = "music" if pick_music else "tech"
    logging.info(f"Выбрана категория: [{primary_cat.upper()}]")

    if pick_music:
        candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
        category = "music"
        if not candidates:
            candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
            category = "tech"
    else:
        candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
        category = "tech"
        if not candidates:
            candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
            category = "music"

    if not candidates:
        logging.info("Новых целевых новостей пока нет.")
        return

    last_domain = ""
    if history:
        last_domain = urlparse(history[-1]).netloc

    diff_domain = [c for c in candidates if c['domain'] != last_domain]
    selected_news = random.choice(diff_domain) if diff_domain else random.choice(candidates)

    logging.info(f"Выбрана новость ({category}): {selected_news['title']}")

    # ЕСЛИ В RSS НЕ БЫЛО КАРТИНКИ — ПАРСИМ СТРАНИЦУ СТАТЬИ В ПОИСКАХ OG:IMAGE
    if not selected_news.get('image_url'):
        logging.info("Картинки в RSS не было, пробую спарсить og:image со страницы...")
        selected_news['image_url'] = fetch_og_image_fallback(selected_news['link'])

    try:
        if category == "music":
            post_text, search_query = generate_music_post(selected_news)
            
            # Если это был конкретный релиз и есть поисковый запрос — ищем стриминги
            if search_query:
                logging.info(f"Ищу ссылки на стриминг для: '{search_query}'...")
                streaming_links = find_streaming_links(search_query, selected_news.get('raw_html', ''))
                reply_markup = build_music_keyboard(streaming_links, selected_news['link'])
            else:
                # Для общих индустриальных новостей делаем только кнопку источника
                reply_markup = build_default_keyboard(selected_news['link'])
        else:
            post_text = generate_tech_post(selected_news)
            reply_markup = build_default_keyboard(selected_news['link'])

        send_to_telegram(
            text=post_text,
            image_url=selected_news.get('image_url'),
            reply_markup=reply_markup,
            target_chat_id=CHANNEL_ID
        )
        
        history.append(selected_news['link'])
        save_history(history)
    except Exception as e:
        logging.error(f"Критическая ошибка публикации: {e}")

if __name__ == "__main__":
    main()
