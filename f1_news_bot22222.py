import os
import json
import asyncio
import aiohttp
import feedparser
import html
import re
import logging
from urllib.parse import urljoin
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import datetime
from bs4 import BeautifulSoup
from collections import deque

# === Настройки ===
BOT_TOKEN = '7829313238:AAENEfOqkYpKLuuq-VNw4tYUs2KNF9z3n3o'
CHANNEL_NAME = '@f1russsia_news'
bot = AsyncTeleBot(BOT_TOKEN)

# === Данные и настройки ===
DATA_FILE = 'data.json'
POSTED_LINKS_MAX = 500

RSS_FEEDS = {
    'F1News.ru': 'https://www.f1news.ru/export/news.xml',
    'Championat': 'https://www.championat.com/rss/news/auto/f1/',
    'Sport-Express (RSS)': 'https://www.sport-express.ru/services/materials/news/formula1/se/',
}

# === Логирование ===
logging.basicConfig(
    filename="f1_news_bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d]: %(message)s"
)
logger = logging.getLogger(__name__)

# === Вспомогательные функции ===

def load_data():
    """Загружает данные из файла."""
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as file:
                data = json.load(file)
                # Используем deque для эффективного управления порядком ссылок
                posted_links = deque(data["posted_links"], maxlen=POSTED_LINKS_MAX)
                return posted_links, data["image_cache"]
        return deque(maxlen=POSTED_LINKS_MAX), {}
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")
        return deque(maxlen=POSTED_LINKS_MAX), {}

def save_data(posted_links, image_cache):
    """Сохраняет данные в файл."""
    try:
        # Преобразуем deque в список для сериализации
        data = {
            "posted_links": list(posted_links),
            "image_cache": image_cache
        }
        with open(DATA_FILE, 'w') as file:
            json.dump(data, file)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

def clean_html(raw_html):
    """Очищает HTML-теги из текста."""
    if not raw_html:
        return ''
    clean_text = re.sub(r'<[^>]+>', '', raw_html)
    clean_text = html.unescape(clean_text)
    return re.sub(r'\s+', ' ', clean_text).strip()

def escape_markdown_v2(text: str) -> str:
    """Экранирует специальные символы Markdown V2."""
    escape_chars = r'_*[]()~`>#+=|{}.!-'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def fetch(session, url):
    """Выполняет GET-запрос к указанному URL."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with session.get(url, headers=headers, timeout=15) as response:
            if response.status != 200:
                logger.warning(f"[HTTP {response.status}] {url}")
                return None
            return await response.text()
    except Exception as e:
        logger.error(f"[FETCH ERROR] {url}: {e}")
        return None

async def extract_image_from_article(session, link, image_cache):
    """Получает изображение из статьи."""
    if link in image_cache:
        return image_cache[link]
    
    try:
        content = await fetch(session, link)
        if not content:
            return None
            
        soup = BeautifulSoup(content, 'html.parser')
        image_url = None
        
        # Сначала ищем изображение по мета-данным (OpenGraph/Twitter Card)
        og_image = soup.find('meta', property='og:image')
        twitter_card = soup.find('meta', attrs={'name': 'twitter:image'})
        
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        elif twitter_card and twitter_card.get('content'):
            image_url = twitter_card['content']
        
        # Если не нашли в мета-тегах, ищем в тегах img
        if not image_url:
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if src:
                    full_url = urljoin(link, src)
                    if any(full_url.lower().endswith(ext) for ext in ['.jpg', '.png', '.jpeg', '.webp']):
                        image_url = full_url
                        break
        
        # Если нашли изображение, кешируем и возвращаем
        if image_url:
            image_cache[link] = image_url
            return image_url
        
    except Exception as e:
        logger.error(f"[IMG ERROR] {link}: {e}")
    
    return None

async def send_news(session, title, link, source, posted_links, image_cache, summary=''):
    """Отправляет новость в канал."""
    # Пропускаем уже опубликованные новости
    if link in posted_links:
        return False
    
    image_url = await extract_image_from_article(session, link, image_cache)
    cleaned_summary = clean_html(summary)
    
    title_md = escape_markdown_v2(title)
    summary_text = escape_markdown_v2(cleaned_summary[:300]) + ('\\...' if len(cleaned_summary) > 300 else '')
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔗 Читать полностью", url=link))
    
    caption = f"🏎 *{title_md}*"
    if summary_text:
        caption += f"\n\n{summary_text}"
    caption += f"\n\n_Источник: {escape_markdown_v2(source)}_"

    try:
        if image_url:
            await bot.send_photo(
                chat_id=CHANNEL_NAME,
                photo=image_url,
                caption=caption,
                reply_markup=markup,
                parse_mode='MarkdownV2'
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_NAME,
                text=caption,
                reply_markup=markup,
                parse_mode='MarkdownV2'
            )
        
        # Добавляем ссылку в опубликованные (deque автоматически управляет размером)
        posted_links.append(link)
        logger.info(f"[PUBLISHED] {source}: {title}")
        return True
    except Exception as e:
        logger.error(f"[SEND ERROR] {link}: {e}")
        return False

# === Основной цикл проверки новостей ===

async def check_news_sources(posted_links, image_cache):
    logger.info(f"Начало проверки источников (сохранено ссылок: {len(posted_links)})")
    total_new = 0
    
    async with aiohttp.ClientSession() as session:
        for source, url in RSS_FEEDS.items():
            try:
                logger.info(f"Проверка RSS: {source}")
                xml = await fetch(session, url)
                if not xml:
                    continue
                
                feed = feedparser.parse(xml)
                new_count = 0
                
                # Ограничиваем количество публикаций до первых 10 записей
                for entry in feed.entries[:10]:
                    title = getattr(entry, 'title', 'Без названия')
                    link = getattr(entry, 'link', '')
                    summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
                    
                    if link and link not in posted_links:
                        success = await send_news(
                            session, title, link, source, 
                            posted_links, image_cache, summary
                        )
                        if success:
                            new_count += 1
                        await asyncio.sleep(1)  # Небольшая пауза между отправками
                
                logger.info(f"RSS {source}: Новых {new_count}/{len(feed.entries[:10])}")
                total_new += new_count
                
            except Exception as e:
                logger.error(f"[RSS ERROR] {source}: {e}")
    
    return total_new

# === Главный цикл мониторинга ===

async def news_monitor():
    # Загрузка начальных данных
    posted_links, image_cache = load_data()
    
    # Первая проверка при старте
    new_count = await check_news_sources(posted_links, image_cache)
    logger.info(f"🚀 Первая проверка завершена. Новых новостей: {new_count}")
    
    # Циклическая проверка каждую минуту
    while True:
        try:
            logger.info("🔄 Начало цикла проверки новостей")
            start_time = datetime.datetime.now()
            
            new_count = await check_news_sources(posted_links, image_cache)
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            
            logger.info(f"✅ Проверка завершена за {elapsed:.1f} сек. Новых: {new_count}")
            logger.info(f"⏳ Следующая проверка через 60 сек. Сохранено ссылок: {len(posted_links)}")
            
            # Периодическое сохранение данных
            save_data(posted_links, image_cache)
            
        except Exception as e:
            logger.error(f"‼️ Ошибка в основном цикле: {e}")
        
        await asyncio.sleep(60)

# === Запуск бота ===

if __name__ == '__main__':
    # Создание нового event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        print("🚀 Бот запущен. Начинаю мониторинг новостей F1...")
        print("Источники:")
        for source in RSS_FEEDS.keys():
            print(f"- {source}")
        
        # Запуск основного цикла мониторинга
        monitor_task = loop.create_task(news_monitor())
        
        # Запуск бесконечного опроса Telegram API
        loop.run_until_complete(bot.infinity_polling())
        
    except KeyboardInterrupt:
        print("\n⛔ Бот остановлен пользователем")
    finally:
        # Сохраняем данные перед выходом
        posted_links, image_cache = load_data()
        save_data(posted_links, image_cache)
        
        if not loop.is_closed():
            loop.close()
        print("✅ Данные сохранены. Работа завершена.")