import os
import re
import json
import logging
import random
import datetime
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

# === РАЗДЕЛЕНИЕ ИСТОЧНИКОВ: МУЗЫКА И ТЕХНИКА ===

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
    """Удаляет UTM-метки и трекеры, чтобы один и тот же URL не постился дважды."""
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

# === ГЕНЕРАЦИЯ ПОСТОВ ПОД КАТЕГОРИИ ===

def generate_music_post(news_item):
    """Промпт с упором на музыку, вайб, стиль и эмоции от прослушивания"""
    prompt = f"""
Ты — музыкальный журналист, диггер и автор Telegram-канала "Speed of Sound" (@speed_sound).
Перед тобой новость о музыкальном релизе, треке или альбоме.
Сделай яркий, вкусный обзор, интересный как обычному слушателю, так и битмейкеру.

ОРИГИНАЛЬНАЯ НОВОСТЬ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА:
1. Заголовок: Цепляющий, стильный, на русском языке. Оберни его в тег <b>...</b>.
2. Первый абзац: Кто дропнул, что вышло (альбом, сингл, клип), в каком жанре/звучании.
3. Второй абзац: Вайб и звук — как это звучит, что цепляет (бит, бас, вокал, атмосфера), почему стоит добавить в плейлист прямо сейчас. Пиши сочно, без воды и канцелярита!
4. Длина: строго до 650 символов!
5. Хештеги в конце (2-3 шт): #релиз #новинка #хипхоп #электроника #слушать #speedofsound
6. Разрешены ТОЛЬКО HTML-теги <b> и <i>. Никаких звездочек Markdown.

Напиши пост:
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

def generate_tech_post(news_item):
    """Промпт для полезного оборудования и софта (30% постов)"""
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
    # Для пятничного дайджеста собираем преимущественно релизы
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

# === ОТПРАВКА В TELEGRAM ===

def send_to_telegram(text, image_url=None, news_link=None, target_chat_id=CHANNEL_ID):
    if news_link:
        text = f"{text}\n\n<a href='{news_link}'>🔗 Источник</a>"
        
    url_base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    
    if image_url and len(text) <= 1024:
        res = requests.post(url_base + "sendPhoto", json={
            "chat_id": target_chat_id,
            "photo": image_url,
            "caption": text,
            "parse_mode": "HTML"
        })
        if res.status_code == 200:
            logging.info("Пост с фото опубликован.")
            return True
        else:
            logging.warning(f"Не удалось отправить фото: {res.text}. Пробую текстом...")

    res = requests.post(url_base + "sendMessage", json={
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": (news_link is None)
    })
    
    if res.status_code == 200:
        logging.info("Текстовый пост опубликован.")
        return True
    else:
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
                    candidates.append({
                        "title": title,
                        "summary": clean_summary[:800],
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
    # Гарантируем наличие необходимых файлов
    if not os.path.exists(DIGEST_STATE_FILE):
        with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
            f.write("")

    if not all([BOT_TOKEN, CHANNEL_ID, AI_API_KEY]):
        logging.error("Отсутствуют обязательные токены!")
        return

    now = datetime.datetime.now()
    is_friday_morning = (now.weekday() == 4) and (8 <= now.hour < 12)

    # 1. ОБРАБОТКА ПЯТНИЧНОГО ДАЙДЖЕСТА В ЛС
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
    
    # Бросаем кубик: 70% вероятность для музыки
    pick_music = random.random() < 0.70
    primary_category = "music" if pick_music else "tech"
    logging.info(f"Бросок вероятности: выбрана категория [{primary_category.upper()}] (70/30 split)")

    if pick_music:
        candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
        category = "music"
        # Если музыки вдруг нет, берем технику как запасной вариант
        if not candidates:
            logging.info("Свежей музыки не нашлось, проверяю софт/железо...")
            candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
            category = "tech"
    else:
        candidates = collect_from_feeds(FEEDS_TECH, KEYWORDS_TECH, history)
        category = "tech"
        # Если техники нет, берем музыку как запасной вариант
        if not candidates:
            logging.info("Свежего софта не нашлось, проверяю музыку...")
            candidates = collect_from_feeds(FEEDS_MUSIC, KEYWORDS_MUSIC, history)
            category = "music"

    if not candidates:
        logging.info("Новых целевых новостей пока нет.")
        return

    # Защита от одного и того же домена подряд
    last_domain = ""
    if history:
        last_domain = urlparse(history[-1]).netloc

    diff_domain = [c for c in candidates if c['domain'] != last_domain]
    selected_news = random.choice(diff_domain) if diff_domain else random.choice(candidates)

    logging.info(f"Выбрана новость ({category}): {selected_news['title']}")

    try:
        # Генерируем пост специализированным промптом
        if category == "music":
            post_text = generate_music_post(selected_news)
        else:
            post_text = generate_tech_post(selected_news)

        send_to_telegram(
            text=post_text,
            image_url=selected_news.get('image_url'),
            news_link=selected_news['link'],
            target_chat_id=CHANNEL_ID
        )
        
        history.append(selected_news['link'])
        save_history(history)
    except Exception as e:
        logging.error(f"Критическая ошибка публикации: {e}")

if __name__ == "__main__":
    main()
