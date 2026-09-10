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

# Переменные окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
AI_API_KEY = os.environ.get("AI_API_KEY")

HISTORY_FILE = "history.json"

# Список проверенных источников RSS (Продвинутый продакшен, плагины, железо, мировой и ру-сегмент)
RSS_FEEDS = [
    "https://samesound.ru/feed",              # Главный ру-портал про продакшен, плагины, DAW и железо
    "https://www.attackmagazine.com/feed/",   # Подробно про продакшен, синтез, битмейкинг и андеграунд
    "https://www.gearnews.com/feed/",         # Оперативные новости про железо, VST, скидки и софт
    "https://www.synthtopia.com/feed/",       # Синтезаторы, новинки софта и железные модули
    "https://www.synthanatomy.com/feed",      # Бесплатные плагины, скидки, железный саунд-дизайн
    "https://mixmag.net/rss.xml",             # Главные мировые релизы и тренды
    "https://edm.com/.rss/full/",             # Релизы, индустрия, крупные инфоповоды
    "https://djmag.com/rss.xml"               # Интервью, железо, топовые релизы
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
        logging.error(f"Ошибка сохранения истории: {e}")

def extract_image_url(entry):
    """Поиск обложки/картинки в RSS элементе"""
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
        html_content = entry.content[0].value
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
    logging.info("Начинаю сбор новостей из RSS...")
    
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.link
                if link in history:
                    continue
                
                title = entry.title
                summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
                clean_summary = BeautifulSoup(summary, "html.parser").get_text()[:700]
                image_url = extract_image_url(entry)
                
                logging.info(f"Найдена новость: {title}. Картинка: {'Да' if image_url else 'Нет'}")
                return {
                    "title": title,
                    "summary": clean_summary,
                    "link": link,
                    "image_url": image_url
                }
        except Exception as e:
            logging.error(f"Ошибка при парсинге {feed_url}: {e}")
            
    return None

def clean_html_for_telegram(text):
    """Очистка HTML для Telegram API"""
    text = re.sub(r'</?(p|div|section|article|header|footer)[^>]*>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?(h1|h2|h3|h4|h5|h6)[^>]*>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def generate_post_with_gemini(news_item):
    genai.configure(api_key=AI_API_KEY)
    
    try:
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        priority = ['models/gemini-3.8-flash', 'models/gemini-3.7-flash', 'models/gemini-3.6-flash', 'models/gemini-2.5-flash']
        selected_model = next((p for p in priority if p in available_models), available_models[0])
    except Exception:
        selected_model = 'models/gemini-2.5-flash'

    model = genai.GenerativeModel(selected_model)
    
    prompt = f"""
Ты — шеф-редактор стильного Telegram-канала для музыкантов, продюсеров, битмейкеров и любителей электронной и хип-хоп культуры в России и СНГ.

Целевая аудитория: люди, которые пилят треки в FL Studio, Ableton, Logic, покупают или качают VST-плагины, интересуются железом, слушают свежие релизы и следят за индустрией.

ОРИГИНАЛ НОВОСТИ:
Заголовок: {news_item['title']}
Текст: {news_item['summary']}

ИНСТРУКЦИЯ ПО НАПИСАНИЮ:
1. ПРИОРИТЕТ ТЕМЫ:
   - Если новость про VST, железо, DAW, сэмплирование или фишки продакшена — сделай упор на ПОЛЬЗУ для музыканта (что за прибор/софт, чем полезен в студии).
   - Если новость про релиз/интервью (электроника, рэп, битмейкинг) — напиши стильно, подчеркни статус артиста или особенность звучания.
   - Если новость про мелкий зарубежный клуб/локальный ивент в США — НЕ зацикливайся на месте проведения, переведи контекст на сам трек, артиста или тренд.

2. СТИЛЬ И ТОН:
   - Экспертный, живой, современный. Без сухого анонса и без глупого кликбейта/спама. 
   - Пиши понятным языком профессионального музыкального комьюнити.

3. ФОРМАТ И ОБЪЕМ:
   - Длина строго от 400 до 650 символов (читается за 15 секунд).
   - <b>Заголовок</b>: 1 яркая жирная строчка с сутью события.
   - Тело поста: 2 коротких емких абзаца.
   - 1-2 аккуратных эмодзи по теме.
   - Разрешены ТОЛЬКО HTML-теги <b> для жирного и <i> для курсива.

Напиши готовую публикацию:
"""

    response = model.generate_content(prompt)
    return clean_html_for_telegram(response.text)

def send_to_telegram(post_text, news_link, image_url=None):
    formatted_text = f"{post_text}\n\n<a href='{news_link}'>Читать источник ↗</a>"
    
    if image_url and len(formatted_text) <= 1000:
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
            logging.warning(f"Не удалось отправить фото ({res.text}). Отправляю текстом...")

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
        logging.error("КРИТИЧЕСКАЯ ОШИБКА: Не заданы переменные окружения!")
        return

    news_item = fetch_fresh_news()
    if not news_item:
        logging.info("Свежих новостей пока нет.")
        return

    logging.info("Генерирую текст поста...")
    try:
        post_text = generate_post_with_gemini(news_item)
    except Exception as e:
        logging.error(f"Ошибка при генерации текста: {e}")
        return

    logging.info("Публикация в Telegram...")
    try:
        send_to_telegram(post_text, news_item['link'], news_item.get('image_url'))
        
        history = load_history()
        history.append(news_item['link'])
        save_history(history)
    except Exception as e:
        logging.error(f"Ошибка при отправке в Telegram: {e}")

if __name__ == "__main__":
    main()
