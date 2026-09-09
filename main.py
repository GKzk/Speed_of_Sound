import os
import json
import logging
import re
import feedparser
import requests
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

HISTORY_FILE = "history.json"

# Расширенный список RSS-лент (рейвы, электронная музыка, железо, VST, битмейкинг и RU-сегмент)
RSS_FEEDS = [
    # Мировые новости электронной сцены и рейв-культуры
    "https://mixmag.net/feed",
    "https://edm.com/.rss/full",
    "https://djmag.com/rss.xml",
    "https://www.attackmagazine.com/feed/",
    
    # Железо, синтезаторы, плагины и софт (Global)
    "https://www.synthtopia.com/feed/",
    "https://synthanatomy.com/feed",
    "https://www.gearnews.com/feed/",
    
    # Русскоязычный сегмент (Музыкальный продакшен, VST, софт, студия, битмейкинг)
    "https://samesound.ru/feed"
]

def load_history() -> list:
    """Загружает историю уже опубликованных ссылок."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки истории: {e}")
            return []
    return []

def save_history(history: list):
    """Сохраняет обновленную историю ссылок."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения истории: {e}")

def clean_html_for_telegram(text: str) -> str:
    """Очищает текст от тегов, не поддерживаемых Telegram HTML API."""
    if not text:
        return ""
    # Заменяем теги абзацев и переносов на новые строки
    text = re.sub(r'</?(p|div|br|h[1-6])\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Оставляем только базовые теги Telegram
    allowed_tags = r'</?(?:b|i|a|code)(?:\s+[^>]*)?>'
    
    def tag_cleaner(match):
        tag = match.group(0)
        if re.match(allowed_tags, tag, re.IGNORECASE):
            return tag
        return ''
        
    cleaned = re.sub(r'</?[^>]+>', tag_cleaner, text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def fetch_fresh_news() -> dict | None:
    """Проходит по RSS-лентам и ищет самую свежую неопубликованную новость."""
    history = load_history()
    logging.info("Начинаю сбор новостей из RSS...")

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue

            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in history:
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))

                # Поиск изображения в RSS
                image_url = None
                if "media_content" in entry and len(entry.media_content) > 0:
                    image_url = entry.media_content[0].get("url")
                elif "media_thumbnail" in entry and len(entry.media_thumbnail) > 0:
                    image_url = entry.media_thumbnail[0].get("url")
                elif "enclosures" in entry:
                    for enc in entry.enclosures:
                        if enc.get("type", "").startswith("image/"):
                            image_url = enc.get("href")
                            break

                if not image_url and summary:
                    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
                    if img_match:
                        image_url = img_match.group(1)

                logging.info(f"Найдена свежая новость: {title}. Картинка: {'Да' if image_url else 'Нет'}")

                return {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "image_url": image_url
                }
        except Exception as e:
            logging.error(f"Ошибка парсинга ленты {feed_url}: {e}")
            continue

    logging.info("Новых новостей не найдено.")
    return None

def get_working_model() -> str:
    """Динамически находит наилучшую доступную модель Gemini."""
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        logging.info(f"Доступные модели: {models}")
        
        preferred_patterns = [
            r'gemini-3\.\d+-flash$',
            r'gemini-3\.\d+-flash',
            r'gemini-3-flash',
            r'gemini-2\.5-flash$',
            r'gemini-flash'
        ]
        
        for pattern in preferred_patterns:
            for model_name in models:
                if re.search(pattern, model_name):
                    logging.info(f"Используем модель: {model_name}")
                    return model_name
                    
        if models:
            selected = models[0]
            logging.info(f"Используем доступную модель по умолчанию: {selected}")
            return selected
            
    except Exception as e:
        logging.warning(f"Не удалось получить список моделей: {e}. Используем запасной вариант.")
        
    return "models/gemini-1.5-flash"

def generate_post_with_gemini(news_item: dict) -> str:
    """Генерирует краткий, емкий и сочный пост через Gemini AI."""
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        raise ValueError("AI_API_KEY не задан в переменных окружения!")

    genai.configure(api_key=api_key)
    model_name = get_working_model()
    model = genai.GenerativeModel(model_name)

    prompt = f"""
Ты — ведущий редактор телеграм-канала о рейвах, электронной музыке, железе, VST-плагинах и битмейкинге.
Напиши КРАТКИЙ, ХЛЁСТКИЙ и СВЕЖИЙ пост по новости ниже.

ТРЕБОВАНИЯ К ПОСТУ:
1. ДЛИНА: Краткий текст, максимум 250-350 символов (2-3 предложения). Без длинных вступлений и воды.
2. СТИЛЬ: Живой, энергичный, инсайдерский (для рейверов, диджеев и продюсеров). При необходимости упоминай конкретные жанры (Techno, D&B, House, Ambient, Trance и др.).
3. ФОРМАТИРОВАНИЕ: Используй ТОЛЬКО HTML-теги <b>для жирного</b> и <i>для курсива</i>. НЕ используй маркдаун (** или *), НЕ используй <p>, <div>, <br>.
4. ЭМОДЗИ: Не более 2-3 стильных эмодзи по теме.
5. ССЫЛКИ: НЕ вставляй ссылку и призывы в сам текст (ссылка будет оформлена кнопкой внизу).

НОВОСТЬ ДЛЯ ОБРАБОТКИ:
Заголовок: {news_item['title']}
Содержимое: {news_item['summary']}
"""

    response = model.generate_content(prompt)
    post_text = clean_html_for_telegram(response.text.strip())
    return post_text

def send_to_telegram(post_text: str, news_url: str, image_url: str | None = None):
    """Отправляет пост в Telegram-канал с кнопкой-ссылкой на источник."""
    bot_token = os.environ.get("BOT_TOKEN")
    channel_id = os.environ.get("CHANNEL_ID")

    if not bot_token or not channel_id:
        raise ValueError("BOT_TOKEN или CHANNEL_ID не заданы!")

    # Аккуратная и стильная кнопка-ссылка
    reply_markup = {
        "inline_keyboard": [
            [{"text": "Читать источник ↗", "url": news_url}]
        ]
    }

    payload = {
        "chat_id": channel_id,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup)
    }

    if image_url:
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        payload["photo"] = image_url
        payload["caption"] = post_text
    else:
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload["text"] = post_text

    response = requests.post(endpoint, data=payload, timeout=15)
    
    # Фолбэк: если ссылка на картинку не сработала, отправляем просто текст
    if not response.ok and image_url:
        logging.warning("Не удалось отправить фото, пробуем отправить только текст...")
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload.pop("photo", None)
        payload.pop("caption", None)
        payload["text"] = post_text
        response = requests.post(endpoint, data=payload, timeout=15)

    if not response.ok:
        logging.error(f"❌ Ошибка отправки в Telegram: {response.status_code}")
        logging.error(f"Ответ API Telegram: {response.text}")
    
    response.raise_for_status()
    logging.info("✅ Пост успешно опубликован в Telegram!")

def main():
    try:
        news_item = fetch_fresh_news()
        if not news_item:
            logging.info("Работа завершена, публикация не требуется.")
            return

        logging.info("Отправляю задачу в Gemini...")
        post_text = generate_post_with_gemini(news_item)

        logging.info("Публикация поста в Telegram...")
        send_to_telegram(post_text, news_item['link'], news_item.get('image_url'))

        # Сохранение ссылки в историю
        history = load_history()
        history.append(news_item['link'])
        save_history(history)

    except Exception as e:
        logging.error(f"Произошла ошибка при выполнении main(): {e}")
        raise e

if __name__ == "__main__":
    main()
