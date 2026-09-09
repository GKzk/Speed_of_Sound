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
                
                logging.info(f"Найдена свежая новость: {title}")
                return {"title": title, "link": link, "summary": summary}
        except Exception as e:
            logging.error(f"Ошибка парсинга ленты {feed_url}: {e}")
    logging.info("Новых статей не найдено.")
    return None

def generate_telegram_post(news_data: dict) -> str | None:
    logging.info("Отправляю задачу в Gemini...")
    
    prompt = (
        "Ты — профессиональный SMM-редактор Telegram-канала о создании электронной музыки и диджеинге. "
        "Твоя задача — перевести предоставленную новость на русский язык и написать крутой пост. \n"
        "Стиль канала: яркий, с юмором, минимум воды, максимум эмоций. \n\n"
        "ТРЕБОВАНИЯ К ПОСТУ:\n"
        "1. Длина текста: строго 400-600 символов.\n"
        "2. Заголовок: жирный текст (в HTML тегах <b>...</b>).\n"
        "3. Эмодзи: 1-2 в начале и в конце поста, а также 1-2 по тексту.\n"
        "4. Структура: короткое вовлекающее интро -> ключевые факты -> авторская подпись.\n"
        "5. Хэштеги: 5-8 подходящих хэштегов в самом конце (например, #electronicmusic #dj #production).\n\n"
        "Верни ТОЛЬКО готовый текст поста в HTML-формате, без лишних комментариев, без markdown-разметки (без ```html).\n\n"
        f"Заголовок: {news_data['title']}\n"
        f"Описание: {news_data['summary']}\n"
    )
    
    try:
        # Получаем список всех доступных моделей
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        logging.info(f"Доступные модели: {available_models}")
        
        if not available_models:
            logging.error("Нет доступных моделей для генерации текста по этому ключу!")
            return None
            
        # Задаем приоритет самых современных моделей, как просит API
        preferred_models = [
            'models/gemini-3.8-flash',
            'models/gemini-3.7-flash',
            'models/gemini-3.6-flash',
            'models/gemini-3.5-flash'
        ]
        
        target_model = available_models[0] # Резервный вариант
        
        # Ищем совпадения с нашим приоритетным списком
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

def send_to_telegram(text: str, link: str):
    logging.info("Публикация поста в Telegram...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    keyboard = {
        "inline_keyboard": [[{"text": "Читать оригинал 🔗", "url": link}]]
    }
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": json.dumps(keyboard)
    }
    
    try:
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

    # Инициализация API
    genai.configure(api_key=AI_API_KEY)

    history = load_history()
    news_item = fetch_fresh_news(history)
    
    if not news_item:
        logging.info("Выполнение завершено, нет новых данных.")
        return
        
    post_text = generate_telegram_post(news_item)
    
    if post_text:
        send_to_telegram(post_text, news_item['link'])
        history.append(news_item['link'])
        save_history(history)
        logging.info("Цикл автоматизации успешно завершен.")

if __name__ == "__main__":
    main()
