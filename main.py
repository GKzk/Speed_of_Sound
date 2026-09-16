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

# Источники с фокусом на электронику, хип-хоп, релизы и продакшен
RSS_FEEDS = [
    "https://the-flow.ru/rss",
    "https://pitchfork.com/rss/reviews/albums/",
    "https://ra.co/xml/news",
    "https://mixmag.net/rss.xml",
    "https://djmag.com/rss.xml",
    "https://hiphopdx.com/rss",
    "https://samesound.ru/feed",
    "https://cdm.link/feed/",
    "https://www.musicradar.com/rss"
]

TARGET_KEYWORDS = [
    'релиз', 'альбом', 'трек', 'album', 'track', 'ep', 'клип', 'video',
    'хип-хоп', 'hip hop', 'рэп', 'rap', 'trap', 'drill', 'электроника', 'electronic',
    'techno', 'house', 'rave', 'synth', 'синтезатор', 'сэмпл', 'producer',
    'ableton', 'daw', 'vst', 'новинка', 'премьера', 'интервью', 'стриминг'
]

# === УТИЛИТЫ ДЛЯ БОРЬБЫ С ДУБЛЯМИ И СОХРАНЕНИЯ СОСТОЯНИЯ ===

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
        # Храним последние 200 ссылок
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

# === ИНТЕЛЛЕКТУАЛЬНЫЙ КАСКАД МОДЕЛЕЙ (ОБХОД ЛИМИТОВ И ОШИБОК) ===

def generate_text_with_fallback(prompt):
    genai.configure(api_key=AI_API_KEY)
    
    # Ниспадающий список моделей: от новых к проверенным
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
                logging.warning(f"[{model_name}] Исчерпан лимит (429 Quota). Переключаюсь на следующую модель...")
            elif '404' in err or 'not found' in err:
                logging.warning(f"[{model_name}] Модель пока не доступна в API. Переключаюсь...")
            else:
                logging.warning(f"[{model_name}] Ошибка: {e}. Переключаюсь...")

    raise RuntimeError("Все доступные модели Gemini вернули ошибки или исчерпали лимиты.")

# === ГЕНЕРАЦИЯ ПОСТОВ ===

def generate_regular_post(news_item):
    prompt = f"""
Ты — куратор и голос Telegram-канала "Speed of Sound" (@speed_sound).
Канал посвящен актуальной музыке: хип-хопу, электронике, новинкам релизов и студийному продакшену. 
Твой стиль: динамичный, дерзкий, профессиональный, но простой и понятный для широкой аудитории (меломанов и битмейкеров).

ОРИГИНАЛЬНАЯ НОВОСТЬ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА ОФОРМЛЕНИЯ:
1. Заголовок: Сделай емкий, цепляющий заголовок на русском языке. Оберни его строго в тег <b>...</b>.
2. Первый абзац: Суть новости (что вышло, кто дропнул трек/альбом, какой софт презентовали).
3. Второй абзац: Оценка и польза (в чем вайб релиза, кому зайдет, как это звучит или почему плагин стоит покрутить). Пиши живым языком без занудства и канцеляризмов.
4. Длина текста: строго до 650 символов!
5. Хештеги в конце (2-3 штуки): #новинка #хипхоп #электроника #продакшен #релиз #vst #speedofsound
6. Форматирование: разрешены ТОЛЬКО HTML-теги <b> и <i>. Никакого Markdown (никаких **звездочек**)!

Напиши пост:
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

def gather_weekly_context():
    weekly_titles = []
    feeds = RSS_FEEDS.copy()
    for feed_url in feeds:
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
Ты — музыкальный редактор Telegram-канала "Speed of Sound". Сегодня пятница — главный день музыкальных новинок!
Вот список релизов и инфоповодов за неделю:
{context}

ЗАДАЧА:
Составь пост: "🔥 Пятничный дайджест: 7-8 главных релизов недели".
Фокус: зарубежный и русскоязычный хип-хоп, свежая электроника (house, techno, bass) и самые обсуждаемые альбомы/синглы.
- Оформи в виде аккуратного пронумерованного списка (1 to 8).
- Укажи Артиста — Название релиза и в 1-2 предложениях опиши, почему это стоит послушать.
- В конце каждого пункта добавь: "Слушать на площадках (Яндекс Музыка, Spotify, VK)".
- Используй жирный шрифт <b>...</b> для названий. Без markdown звездочек. Лимит 2200 знаков.
"""
    raw_text = generate_text_with_fallback(prompt)
    return clean_html_for_telegram(raw_text)

# === ОТПРАВКА В TELEGRAM ===

def send_to_telegram(text, image_url=None, news_link=None, target_chat_id=CHANNEL_ID):
    if news_link:
        text = f"{text}\n\n<a href='{news_link}'>🔗 Источник</a>"
        
    url_base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    
    # 1. Попытка отправить с картинкой
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

    # 2. Отправка сообщением
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

# === ОСНОВНОЙ ЦИКЛ ===

def main():
    if not all([BOT_TOKEN, CHANNEL_ID, AI_API_KEY]):
        logging.error("Отсутствуют обязательные токены в Secrets / Environment!")
        return

    now = datetime.datetime.now()
    # Пятница — это день недели под индексом 4. Время с 8 до 12 утра.
    is_friday_morning = (now.weekday() == 4) and (8 <= now.hour < 12)

    # 1. ОБРАБОТКА ПЯТНИЧНОГО ДАЙДЖЕСТА В ЛС
    if is_friday_morning:
        if not ADMIN_ID:
            logging.warning("Наступило утро пятницы, но ADMIN_ID не задан в Secrets!")
        elif not is_digest_sent_today():
            logging.info("Пятница до обеда: формирую еженедельный дайджест в ЛС админу...")
            try:
                digest = generate_friday_digest()
                notice = (
                    "<b>🔔 ПЯТНИЧНЫЙ ДАЙДЖЕСТ (ЧЕРНОВИК НА СОГЛАСОВАНИЕ)</b>\n\n"
                    f"{digest}\n\n"
                    "<i>Отредактируй текст при необходимости и скопируй в канал.</i>"
                )
                success = send_to_telegram(notice, target_chat_id=ADMIN_ID)
                if success:
                    mark_digest_sent()
                    logging.info("Дайджест успешно доставлен в ЛС.")
            except Exception as e:
                logging.error(f"Не удалось сформировать пятничный дайджест: {e}")
            return
        else:
            logging.info("Пятничный дайджест уже отправлялся сегодня.")

    # 2. РЕГУЛЯРНЫЙ ПОСТИНГ НОВОСТЕЙ
    history = load_history()
    random.shuffle(RSS_FEEDS)
    
    candidates = []

    for feed_url in RSS_FEEDS:
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
                
                if any(kw in text_to_check for kw in TARGET_KEYWORDS):
                    image_url = extract_image_url(entry)
                    candidates.append({
                        "title": title,
                        "summary": clean_summary[:800],
                        "link": link,
                        "image_url": image_url,
                        "domain": urlparse(link).netloc
                    })
                    break 
        except Exception as e:
            logging.error(f"Ошибка при чтении ленты {feed_url}: {e}")

    if not candidates:
        logging.info("Свежих целевых новостей пока нет.")
        return

    # Защита: избегаем публикаций с одного и того же сайта подряд
    last_domain = ""
    if history:
        last_domain = urlparse(history[-1]).netloc

    diff_domain = [c for c in candidates if c['domain'] != last_domain]
    selected_news = random.choice(diff_domain) if diff_domain else random.choice(candidates)

    logging.info(f"Выбрана новость: {selected_news['title']}")
    try:
        post_text = generate_regular_post(selected_news)
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
