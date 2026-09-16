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
    'techno', 'house', 'rave', 'премьера', 'новинка', 'сингл', 'single', 'слушать', 'review'
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

def extract_image_url(entry):
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
            if c.type == 'text/html':
                html_content += c.value
    elif 'description' in entry:
        html_content = entry.description

    if html_content:
        soup = BeautifulSoup(html_content, 'html.parser')
        img = soup.find('img')
        if img and img.get('src') and img['src'].startswith('http'):
            return img['src']
    return None

def clean_html_for_telegram(text):
    text = re.sub(r'</?(p|div|section|article|header|footer|html|body)[^>]*>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?(h1|h2|h3|h4|h5|h6)[^>]*>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text

# === УМНЫЙ ПОИСК СТРИМИНГОВ ЧЕРЕЗ SONGLINK / ODESLI ===

def find_streaming_links(search_query, raw_html=""):
    """
    1. Ищет прямые ссылки на стриминги в тексте статьи.
    2. Если нет — ищет трек через iTunes API.
    3. Полученный URL отправляет в Odesli API для генерации мультиссылки.
    """
    headers = {"User-Agent": "SpeedOfSoundBot/1.0"}
    streaming_url = None

    # 1. Поиск ссылок в тексте новости
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
                logging.info(f"Найдена прямая ссылка на стриминг в новости: {streaming_url}")
                break

    # 2. Поиск через iTunes Search API
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

    # 3. Резолв через Odesli / Songlink
    if streaming_url:
        try:
            odesli_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(streaming_url)}&userCountry=RU"
            res = requests.get(odesli_url, headers=headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                page_url = data.get('pageUrl')
                platforms = data.get('linksByPlatform', {})
                return {
                    "page_url": page_url,
                    "yandex": platforms.get('yandex', {}).get('url'),
                    "spotify": platforms.get('spotify', {}).get('url'),
                    "apple": platforms.get('appleMusic', {}).get('url')
                }
        except Exception as e:
            logging.warning(f"Ошибка Odesli API: {e}")

    # 4. Фолбек: прямые ссылки на поиск в сервисах
    if search_query:
        q_enc = urllib.parse.quote(search_query)
        return {
            "page_url": None,
            "yandex": f"https://music.yandex.ru/search?text={q_enc}",
            "spotify": f"https://open.spotify.com/search/{q_enc}",
            "apple": None
        }

    return None

# === ИНЛАЙН-КНОПКИ (REPLY_MARKUP) ===

def build_music_keyboard(links, news_link):
    keyboard = []
    
    if links:
        # Универсальная мультиссылка (song.link / album.link)
        if links.get("page_url"):
            keyboard.append([
                {"text": "🎧 Слушать релиз (все сервисы)", "url": links["page_url"]}
            ])
            
        # Кнопки быстрого перехода на Яндекс / Spotify в один ряд
        service_row = []
        if links.get("yandex"):
            service_row.append({"text": "🔴 Яндекс Музыка", "url": links["yandex"]})
        if links.get("spotify"):
            service_row.append({"text": "🟢 Spotify", "url": links["spotify"]})
            
        if service_row:
            keyboard.append(service_row)

    # Кнопка первоисточника
    if news_link:
        keyboard.append([
            {"text": "🔗 Читать первоисточник", "url": news_link}
        ])

    return {"inline_keyboard": keyboard} if keyboard else None

def build_tech_keyboard(news_link):
    if not news_link:
        return None
    return {
        "inline_keyboard": [
            [{"text": "🔗 Подробнее / Первоисточник", "url": news_link}]
        ]
    }

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
                logging.info(f"Успешно сгенерировано через {model_name}")
                return response.text
        except Exception as e:
            err = str(e).lower()
            if '429' in err or 'quota' in err or 'exhausted' in err:
                logging.warning(f"[{model_name}] Исчерпан лимит (429 Quota). Переключаюсь...")
            elif '404' in err or 'not found' in err:
                logging.warning(f"[{model_name}] Модель не найдена в API. Переключаюсь...")
            else:
                logging.warning(f"[{model_name}] Ошибка: {e}. Переключаюсь...")

    raise RuntimeError("Все доступные модели Gemini вернули ошибки или исчерпали лимиты.")

# === ГЕНЕРАЦИЯ ПОСТОВ ===

def generate_music_post(news_item):
    prompt = f"""
Ты — музыкальный редактор и диггер Telegram-канала "Speed of Sound" (@speed_sound).
Перед тобой новость о музыкальном релизе/треке/альбоме.
Сделай яркий, стильный пост для широкой аудитории (меломанов и битмейкеров).

ОРИГИНАЛЬНАЯ НОВОСТЬ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА ОФОРМЛЕНИЯ:
1. Заголовок: Сочный, цепляющий, на русском языке в теге <b>...</b>.
2. В первой строке поста под заголовком сделай карточку релиза:
   🎧 <b>Жанр:</b> [Жанр] | <b>Вайб:</b> [2-3 слова об атмосфере]
3. Основной текст (1-2 абзаца): В чем уникальность релиза, как звучит бас/бит/вокал, почему этот трек стоит послушать прямо сейчас. Пиши живо, вкусно, без занудства.
4. Объем текста строго до 650 символов!
5. Хештеги в конце: 2-3 штуки (#новинка #хипхоп #электроника #релиз #speedofsound)
6. Разрешены ТОЛЬКО HTML-теги <b> и <i>. Никакого Markdown (**).
7. В САМОЙ ПОСЛЕДНЕЙ СТРОКЕ ОБЯЗАТЕЛЬНО добавь служебную строку для поиска:
SEARCH: Исполнитель - Название трека или альбома

Напиши пост:
"""
    raw_text = generate_text_with_fallback(prompt)
    
    # Извлекаем поисковый запрос и очищаем пост от служебной строки
    search_query = ""
    cleaned_lines = []
    for line in raw_text.strip().split('\n'):
        if line.strip().startswith('SEARCH:'):
            search_query = line.replace('SEARCH:', '').strip()
        else:
            cleaned_lines.append(line)
            
    final_text = clean_html_for_telegram('\n'.join(cleaned_lines))
    return final_text, search_query

def generate_tech_post(news_item):
    prompt = f"""
Ты — куратор Telegram-канала "Speed of Sound" (@speed_sound) и опытный саунд-продюсер.
Перед тобой новость про софт, плагин, девайс или фишку для продакшена.
Сделай полезный, емкий пост без занудства.

ОРИГИНАЛЬНАЯ НОВОСТЬ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА:
1. Заголовок: Емкий и прикладной, в теге <b>...</b>.
2. Первый абзац: Суть девайса/обновления (что делает этот инструмент или софт).
3. Второй абзац: Реальная польза (как разгоняет воркфлоу, какой звук дает, кому пригодится в сетапе).
4. Длина: строго до 600 символов!
5. Хештеги в конце: #продакшен #vst #железо #ableton #plugins #speedofsound
6. Разрешены ТОЛЬКО HTML-теги <b> и <i>. Никаких звездочек Markdown.

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
Ты — музыкальный редактор канала "Speed of Sound". Сегодня пятница — New Music Friday!
Вот релизы и новости недели:
{context}

ЗАДАЧА:
Составь "🔥 Пятничный дайджест: 7-8 главных релизов недели".
Фокус: зарубежный и русскоязычный хип-хоп, клубная и домашняя электроника, самые обсуждаемые альбомы.
- Пронумерованный список от 1 до 8.
- Артист — Название: в 1-2 предложениях опиши, почему релиз заслуживает внимания.
- После каждого трека добавь: "🎧 Слушать на площадках".
- Используй HTML-тег <b> для названий. Без markdown-звездочек. До 2200 символов.
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

# === ОТПРАВКА В TELEGRAM С ИНЛАЙН-КНОПКАМИ ===

def send_to_telegram(text, image_url=None, reply_markup=None, target_chat_id=CHANNEL_ID):
    url_base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    
    payload = {
        "chat_id": target_chat_id,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    # 1. С фото
    if image_url and len(text) <= 1024:
        payload["photo"] = image_url
        payload["caption"] = text
        res = requests.post(url_base + "sendPhoto", json=payload)
        if res.status_code == 200:
            logging.info("Пост с фото и кнопками успешно опубликован.")
            return True
        logging.warning(f"Не удалось отправить фото: {res.text}. Пробую текстом...")

    # 2. Текстом
    payload.pop("photo", None)
    payload.pop("caption", None)
    payload["text"] = text
    payload["disable_web_page_preview"] = True
    
    res = requests.post(url_base + "sendMessage", json=payload)
    if res.status_code == 200:
        logging.info("Текстовый пост с кнопками успешно опубликован.")
        return True
    
    logging.error(f"Ошибка отправки сообщения: {res.text}")
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
                    # Сохраняем raw HTML для поиска ссылок на стриминги
                    raw_html = ""
                    if 'content' in entry:
                        for c in entry.content:
                            raw_html += c.value
                    raw_html += (summary or '')

                    candidates.append({
                        "title": title,
                        "summary": clean_summary[:800],
                        "raw_html": raw_html,
                        "link": link,
                        "image_url": extract_image_url(entry),
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
        logging.error("Отсутствуют обязательные токены!")
        return

    now = datetime.datetime.now()
    is_friday_morning = (now.weekday() == 4) and (8 <= now.hour < 12)

    # 1. ПЯТНИЧНЫЙ ДАЙДЖЕСТ
    if is_friday_morning:
        if not ADMIN_ID:
            logging.warning("Пятница утро, но ADMIN_ID не задан!")
        elif not is_digest_sent_today():
            logging.info("Пятница до обеда: отправка дайджеста в ЛС...")
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
                    logging.info("Дайджест отправлен админу.")
            except Exception as e:
                logging.error(f"Ошибка дайджеста: {e}")
            return
        else:
            logging.info("Дайджест уже отправлялся сегодня.")

    # 2. РЕГУЛЯРНЫЙ ПОСТИНГ: 70% МУЗЫКА, 30% ТЕХНИКА
    history = load_history()
    
    pick_music = random.random() < 0.70
    primary_category = "music" if pick_music else "tech"
    logging.info(f"Бросок вероятности: выбрана категория [{primary_category.upper()}] (70/30 split)")

    if pick_music:
        candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
        category = "music"
        if not candidates:
            logging.info("Свежей музыки не нашлось, проверяю софт/железо...")
            candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
            category = "tech"
    else:
        candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
        category = "tech"
        if not candidates:
            logging.info("Свежего софта не нашлось, проверяю музыку...")
            candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
            category = "music"

    if not candidates:
        logging.info("Новых целевых новостей пока нет.")
        return

    # Ротация доменов
    last_domain = ""
    if history:
        last_domain = urlparse(history[-1]).netloc

    diff_domain = [c for c in candidates if c['domain'] != last_domain]
    selected_news = random.choice(diff_domain) if diff_domain else random.choice(candidates)

    logging.info(f"Выбрана новость ({category}): {selected_news['title']}")

    try:
        reply_markup = None
        
        if category == "music":
            post_text, search_query = generate_music_post(selected_news)
            logging.info(f"Ищу ссылки на стриминг для: '{search_query}'...")
            streaming_links = find_streaming_links(search_query, selected_news.get('raw_html', ''))
            reply_markup = build_music_keyboard(streaming_links, selected_news['link'])
        else:
            post_text = generate_tech_post(selected_news)
            reply_markup = build_tech_keyboard(selected_news['link'])

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
