import os
import re
import json
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
AI_API_KEY = os.environ.get("AI_API_KEY")

HISTORY_FILE = "history.json"

# Расширенный список (добавлен эксклюзивный западный сегмент)
RSS_FEEDS = [
    "https://cdm.link/feed/",                 # Create Digital Music (очень крутой гиковский ресурс)
    "https://www.musicradar.com/rss",         # Обзоры софта, плагинов, техники
    "https://www.attackmagazine.com/feed/",   
    "https://www.gearnews.com/feed/",         
    "https://www.synthtopia.com/feed/",       
    "https://www.synthanatomy.com/feed",      
    "https://mixmag.net/rss.xml",             
    "https://djmag.com/rss.xml",              
    "https://samesound.ru/feed"               # Оставляем, но теперь фильтруем
]

# Жесткий фильтр ценности. Новость берется ТОЛЬКО если содержит эти слова (на англ или рус)
TARGET_KEYWORDS = [
    'vst', 'plugin', 'плагин', 'ableton', 'fl studio', 'logic pro', 'cubase', 'daw',
    'synth', 'синтезатор', 'драм-машина', 'сэмпл', 'sample', 'midi', 'миди',
    'обновление', 'update', 'сведение', 'мастеринг', 'mixing', 'битмейкинг',
    'релиз', 'album', 'release', 'интервью', 'interview', 'free', 'бесплатно'
]

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения истории: {e}")
    return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-100:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения: {e}")

def extract_image_url(entry):
    """Усиленный поиск картинки по всем возможным тегам"""
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url')
        
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                return enc.get('href')

    # Глубокий поиск в HTML контенте (часто решает проблему с ру-сайтами)
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
        if img and img.get('src'):
            src = img['src']
            if src.startswith('http'):
                return src
    return None

def fetch_fresh_news():
    history = load_history()
    logging.info("Начинаю сбор новостей с фильтрацией...")
    
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.link
                if link in history:
                    continue
                
                title = entry.title
                summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
                clean_summary = BeautifulSoup(summary, "html.parser").get_text()
                
                # ФИЛЬТРАЦИЯ: Проверяем, есть ли ценность в новости
                text_to_check = (title + " " + clean_summary).lower()
                if not any(kw in text_to_check for kw in TARGET_KEYWORDS):
                    # Если тема проходная, помечаем в историю чтобы больше не проверять, и пропускаем
                    history.append(link)
                    save_history(history)
                    continue

                image_url = extract_image_url(entry)
                logging.info(f"Найдена ЦЕЛЕВАЯ новость: {title}. Картинка: {'Да' if image_url else 'Нет'}")
                return {
                    "title": title,
                    "summary": clean_summary[:800],
                    "link": link,
                    "image_url": image_url
                }
        except Exception as e:
            logging.error(f"Ошибка при парсинге {feed_url}: {e}")
            
    return None

def clean_html_for_telegram(text):
    text = re.sub(r'</?(p|div|section|article|header|footer)[^>]*>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?(h1|h2|h3|h4|h5|h6)[^>]*>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def generate_post_with_gemini(news_item):
    genai.configure(api_key=AI_API_KEY)
    
    # Список моделей по приоритету (если одна исчерпала лимит — пробуем следующую)
    candidate_models = [
        'gemini-2.5-flash',
        'gemini-1.5-flash',
        'gemini-2.0-flash',
        'gemini-3.8-flash'
    ]

    prompt = f"""
Ты — куратор и эксперт Telegram-канала для битмейкеров, саунд-продюсеров и музыкантов, а также музыкальный журналист и ценитель качественной электронной музыки, внимательно следящий за новыми интересными релизами и трендами сцены.
Твоя задача — дать выжимку самого важного из новости и сделать полезный, стильный пост.

ОРИГИНАЛ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ПРАВИЛА:
1. Сделай <b>цепляющий, но строгий заголовок</b>.
2. В первом абзаце — суть новости (что вышло/что случилось).
3. Во втором абзаце — добавочная ценность (аналитика): как это повлияет на продакшен, почему этот релиз/софт заслуживает внимания, практическая польза для музыканта.
4. Пиши профессиональным, знающим, но живым языком. Без лишней воды.
5. Строго до 650 символов!
6. В самом конце поста обязательно добавь 2-3 релевантных хештега СТРОГО из этого списка: 
   #vst #plugins #daw #ableton #flstudio #freebies #железо #hardware #synths #синтез #drummachine #продакшен #production #sounddesign #mixing #mastering #beatmaking #samples #релизы #releases #news #interview
7. Разрешены только HTML-теги <b> (жирный) и <i> (курсив).

Напиши пост:
"""

    for model_name in candidate_models:
        try:
            logging.info(f"Пробую сгенерировать пост через модель: {model_name}")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return clean_html_for_telegram(response.text)
        except Exception as e:
            logging.warning(f"Модель {model_name} выдала ошибку лимита/доступа ({e}). Перехожу к следующей...")

    raise RuntimeError("Все доступные модели Gemini исчерпали дневной лимит!")


def send_to_telegram(post_text, news_link, image_url=None):
    formatted_text = f"{post_text}\n\n<a href='{news_link}'>Читать источник ↗</a>"
    
    if image_url and len(formatted_text) <= 1024:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": CHANNEL_ID,
            "photo": image_url,
            "caption": formatted_text,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            logging.info("Пост с фото успешно опубликован!")
            return
        else:
            logging.warning(f"Ошибка фото ({res.text}). Пробую текстом...")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": formatted_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    res = requests.post(url, json=payload)
    res.raise_for_status()
    logging.info("Текстовый пост успешно опубликован!")

def main():
    if not all([BOT_TOKEN, CHANNEL_ID, AI_API_KEY]):
        logging.error("Нет токенов!")
        return

    news_item = fetch_fresh_news()
    if not news_item:
        logging.info("Свежих целевых новостей пока нет.")
        return

    logging.info("Генерирую текст поста...")
    try:
        post_text = generate_post_with_gemini(news_item)
        send_to_telegram(post_text, news_item['link'], news_item.get('image_url'))
        
        history = load_history()
        history.append(news_item['link'])
        save_history(history)
    except Exception as e:
        logging.error(f"Ошибка: {e}")

if __name__ == "__main__":
    main()
