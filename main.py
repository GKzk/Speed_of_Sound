import os
import json
import logging
import feedparser
import requests
import google.generativeai as genai

# Настройка логирования для отслеживания процесса в GitHub Actions
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Получаем ключи из секретов GitHub
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")

# Файл для сохранения истории опубликованных ссылок
HISTORY_FILE = "history.json"

# Список RSS-лент
RSS_FEEDS = [
    "https://mixmag.net/feed",
    "https://edm.com/.rss/full",
    "https://djmag.com/rss.xml"
]

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения истории: {e}")
            return []
    return []

def save_history(history: list):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-200:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения истории: {e}")

def fetch_fresh_news(history: list) -> dict | None:
    logging.info("Начинаю сбор новостей из RSS...")
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.get("link", "")
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                
                if link in history:
                    continue
                
                # Пытаемся вытащить картинку (обложку релиза или фото статьи) из RSS
                image_url = ""
                if "media_content" in entry and len(entry.media_content) > 0:
                    image_url = entry.media_content[0].get("url", "")
                elif "enclosures" in entry and len(entry.enclosures) > 0:
                    for enc in entry.enclosures:
                        if "image" in enc.get("type", ""):
                            image_url = enc.get("href", "")
                            break
                
                logging.info(f"Найдена свежая новость: {title}. Картинка: {'Да' if image_url else 'Нет'}")
                return {"title": title, "link": link, "summary": summary, "image_url": image_url}
        except Exception as e:
            logging.error(f"Ошибка парсинга ленты {feed_url}: {e}")
    logging.info("Новых статей не найдено.")
    return None

def generate_telegram_post(news_data: dict) -> str | None:
    logging.info("Отправляю задачу в Gemini...")
    
    prompt = (
        "Ты — инсайдер рейв-индустрии, музыкальный продюсер и автор крутого Telegram-канала об электронной музыке. "
        "Твоя аудитория — рейверы, диджеи и саунд-продюсеры. Твоя задача — написать живой, цепляющий и сочный пост из предложенной новости.\n\n"
        "СТИЛЬ И ПОДАЧА:\n"
        "- Энергично, легко для чтения (используй абзацы), с журналистским фактажом, но без занудства и без спам-кликбейта.\n"
        "- Текст должен дышать культурой танцпола. Если исходная новость скучная — сделай ее интересной!\n"
        "- Обязательно упоминай конкретные жанры (Techno, House, Drum & Bass, Trance, IDM и т.д.), если они подходят по смыслу.\n"
        "- Если новость о новом плагине, VST, синтезаторе или релизе трека — сделай акцент на том, чтобы читатели перешли по ссылке в кнопке внизу поста (например: 'Качайте плагин по ссылке ниже', 'Забирайте релиз').\n\n"
        "СТРУКТУРА:\n"
        "1. Заголовок: Сочный, привлекающий внимание (в HTML тегах <b>...</b>).\n"
        "2. Интро: Одно предложение, чтобы зацепить.\n"
        "3. Суть: Абзац с 'мясом' новости. Максимум полезной инфы простым языком.\n"
        "4. Эмодзи: Используй стильные эмодзи (🔥, 🎛, 👽, 🔊), но не больше 3-4 на весь пост.\n"
        "5. Хэштеги: 3-4 штуки в самом конце.\n\n"
        "ОБЪЕМ: 500-800 символов. Текст должен быть компактным.\n"
        "ВЕРНИ ТОЛЬКО ГОТОВЫЙ ТЕКСТ В HTML. БЕЗ markdown-тегов ```html.\n\n"
        f"Заголовок: {news_data['title']}\n"
        f"Описание: {news_data['summary']}\n"
    )
    
    try:
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        logging.info(f"Доступные модели: {available_models}")
        
        if not available_models:
            logging.error("Нет доступных моделей для генерации текста по этому ключу!")
            return None
            
        preferred_models = [
            'models/gemini-3.8-flash',
            'models/gemini-3.7-flash',
            'models/gemini-3.6-flash'
        ]
        
        target_model = available_models[0]
        for pref in preferred_models:
            if pref in available_models:
                target_model = pref
                break
                
        logging.info(f"Используем модель: {target_model}")
        
        model = genai.GenerativeModel(target_model)
        response = model.generate_content(prompt)
        
        post_text = response.text.replace("```html", "").replace("```", "").strip()
        return post_text
    except Exception as e:
        logging.error(f"Критическая ошибка при генерации текста Gemini: {e}")
        return None

def send_to_telegram(text: str, link: str, image_url: str = ""):
    logging.info("Публикация поста в Telegram...")
    
    # Кнопка со ссылкой на источник / плагин
    keyboard = {
        "inline_keyboard": [[{"text": "⚡️ Читать / Качать / Смотреть", "url": link}]]
    }
    
    # Telegram API лимитирует подпись к фото до 1024 символов
    if image_url and len(text) > 1000:
        text = text[:990] + "..."
        
    try:
        # Если есть картинка - отправляем фото, если нет - обычное сообщение
        if image_url:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            payload = {
                "chat_id": CHANNEL_ID,
                "photo": image_url,
                "caption": text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            }
        else:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHANNEL_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": json.dumps(keyboard)
            }
            
        response = requests.post(url, data=payload)
        response.raise_for_status()
        logging.info("✅ Пост успешно опубликован!")
    except Exception as e:
        logging.error(f"❌ Ошибка отправки в Telegram: {e}")
        if 'response' in locals():
            logging.error(f"Ответ API Telegram: {response.text}")
        raise e

def main():
    if not all([BOT_TOKEN, CHANNEL_ID, AI_API_KEY]):
        logging.error("КРИТИЧЕСКАЯ ОШИБКА: Не заданы переменные окружения!")
        return

    genai.configure(api_key=AI_API_KEY)

    history = load_history()
    news_item = fetch_fresh_news(history)
    
    if not news_item:
        logging.info("Выполнение завершено, нет новых данных.")
        return
        
    post_text = generate_telegram_post(news_item)
    
    if post_text:
        send_to_telegram(post_text, news_item['link'], news_item.get('image_url', ''))
        history.append(news_item['link'])
        save_history(history)
        logging.info("Цикл автоматизации успешно завершен.")

if __name__ == "__main__":
    main()
