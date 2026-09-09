# ... existing code ...
# Файл для сохранения истории опубликованных ссылок
HISTORY_FILE = "history.json"

# Список RSS-лент (рейвы, электронная музыка, железо, VST, битмейкинг и RU-сегмент)
RSS_FEEDS = [
    # Мировые новости электронной сцены и рейв-культуры
    "https://mixmag.net/feed",
    "https://edm.com/.rss/full",
    "https://djmag.com/rss.xml",
    "https://www.attackmagazine.com/feed/",
    
    # Железо, синтезаторы, плагины и битмейкинг (Global)
    "https://www.synthtopia.com/feed/",
    "https://synthanatomy.com/feed",
    "https://www.gearnews.com/feed/",
    
    # Русскоязычный сегмент (Музыкальный продакшен, VST, софт, студия)
    "https://samesound.ru/feed"
]

def load_history() -> list:
# ... existing code ...
```

### Что изменится в канале:
* Появятся анонсы свежих бесплатностей и скидок на плагины (VST).
* Обзоры нового железа (синтезаторы, драм-машины, контроллеры).
* Новости битмейкинга и звукорежиссуры на русском языке из **SAMESOUND**.
* Бот будет циклично проходить по всем 8 лентам, находить самую свежую новость и делать из нее краткий пост!
