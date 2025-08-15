import os
import json
import asyncio
import logging
import datetime
import html
import re
import signal
import random
from collections import deque
from contextlib import suppress
from typing import Deque, Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================
# Конфигурация
# ==========================

BOT_TOKEN = "7829313238:AAENEfOqkYpKLuuq-VNw4tYUs2KNF9z3n3o"
CHANNEL_NAME = "@f1russsia_news"

DATA_FILE = "data.json"
POSTED_LINKS_MAX = 1000
CHECK_INTERVAL_SECONDS = 60

TELEGRAM_MAX_CAPTION = 1024
TELEGRAM_PARSE_MODE = "MarkdownV2"

HEADLINE_ICON = "🏎"
PRIMARY_BUTTON_PREFIX = "Открыть на"
IOS_SUMMARY_MAX = 420

RSS_FEEDS = {
    "F1News.ru": "https://www.f1news.ru/export/news.xml",
    "Championat": "https://www.championat.com/rss/news/auto/f1/",
    "Sport-Express (RSS)": "https://www.sport-express.ru/services/materials/news/formula1/se/",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

bot = AsyncTeleBot(BOT_TOKEN, parse_mode=TELEGRAM_PARSE_MODE)

logging.basicConfig(
    filename="f1_news_bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d]: %(message)s",
)
logger = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "yclid", "fbclid", "_openstat", "utm_referrer", "ref", "from",
    "tgShare", "tgs",
}

# ==========================
# Вспомогательные функции
# ==========================

def load_data() -> Tuple[Deque[str], Dict[str, str]]:
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
                posted_links = deque(data.get("posted_links", []), maxlen=POSTED_LINKS_MAX)
                image_cache = dict(data.get("image_cache", {}))
                return posted_links, image_cache
        return deque(maxlen=POSTED_LINKS_MAX), {}
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")
        return deque(maxlen=POSTED_LINKS_MAX), {}


def save_data(posted_links: Deque[str], image_cache: Dict[str, str]) -> None:
    try:
        tmp_path = f"{DATA_FILE}.tmp"
        data = {
            "posted_links": list(posted_links),
            "image_cache": image_cache,
        }
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        qpairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if k.lower() not in TRACKING_PARAMS]
        qpairs.sort()
        query = urlencode(qpairs, doseq=True)
        return urlunsplit((scheme, netloc, parts.path or "", query, ""))
    except Exception:
        return url


def extract_domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return "источник"


def clean_html(text: Optional[str]) -> str:
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        cleaned = " ".join(soup.stripped_strings)
    except Exception:
        cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


_MD_V2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def escape_markdown_v2(text: str) -> str:
    if not text:
        return ""
    return _MD_V2_ESCAPE_RE.sub(r"\\\1", text)


def safe_truncate_mdv2(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    while truncated.endswith("\\"):
        truncated = truncated[:-1]
    return truncated


def trim_summary_for_ios(text: str, target_len: int = IOS_SUMMARY_MAX) -> str:
    if len(text) <= target_len:
        return text
    candidates = []
    for ch in (".", "!", "?", "…"):
        pos = text.rfind(ch, 0, target_len + 1)
        if pos != -1:
            candidates.append(pos)
    if candidates:
        return text[:max(candidates) + 1].strip()
    space_pos = text.rfind(" ", 0, target_len + 1)
    return (text[:space_pos] + "…").strip() if space_pos != -1 else (text[:target_len - 1] + "…").strip()


def format_time_ru(dt: datetime.datetime) -> str:
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    return f"{dt.strftime('%H:%M')} · {dt.day} {months[dt.month - 1]}"


async def fetch(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"[HTTP {resp.status}] {url}")
                return None
            return await resp.text()
    except aiohttp.ClientError as e:
        logger.error(f"[NETWORK ERROR] {url}: {e}")
        return None
    except asyncio.TimeoutError:
        logger.error(f"[TIMEOUT] {url}")
        return None


def is_valid_image_url(url: str) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        path = parts.path.lower()
        if path.endswith((".jpg", ".jpeg", ".png", ".webp")):
            if any(x in path for x in ["logo", "favicon", "sprite"]):
                return False
            return True
        return False
    except Exception:
        return False


def select_image_from_entry(entry, base_link: str) -> Optional[str]:
    with suppress(Exception):
        media_content = getattr(entry, "media_content", None) or entry.get("media_content")
        if media_content and isinstance(media_content, list):
            for m in media_content:
                url = (m or {}).get("url")
                if url:
                    candidate = urljoin(base_link, url)
                    if is_valid_image_url(candidate):
                        return candidate
    with suppress(Exception):
        links = getattr(entry, "links", None) or entry.get("links")
        if links and isinstance(links, list):
            for lnk in links:
                if ((lnk or {}).get("rel") == "enclosure" and
                    "image" in (((lnk or {}).get("type") or ""))):
                    href = (lnk or {}).get("href")
                    if href:
                        candidate = urljoin(base_link, href)
                        if is_valid_image_url(candidate):
                            return candidate
    return None


async def extract_image_from_article(session: aiohttp.ClientSession, link: str, image_cache: Dict[str, str]) -> Optional[str]:
    if link in image_cache:
        cached = image_cache[link]
        if is_valid_image_url(cached):
            return cached
        else:
            with suppress(Exception):
                del image_cache[link]
    content = await fetch(session, link)
    if not content:
        return None
    soup = BeautifulSoup(content, "html.parser")
    candidates = []
    for prop in ("og:image:secure_url", "og:image"):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            candidates.append(tag["content"])
    for name in ("twitter:image:src", "twitter:image"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            candidates.append(tag["content"])
    if not candidates:
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if src:
                candidates.append(src)
                break
    for url in candidates:
        full = urljoin(link, url)
        if is_valid_image_url(full):
            image_cache[link] = full
            return full
    return None


def build_caption(title: str, source_name: str, summary: str) -> str:
    title_md = escape_markdown_v2(title)
    header = f"{HEADLINE_ICON} *{title_md}*"
    summary_md = escape_markdown_v2(trim_summary_for_ios(clean_html(summary), IOS_SUMMARY_MAX)) if summary else ""
    footer_md = escape_markdown_v2(f"🕒 {format_time_ru(datetime.datetime.now())} · {source_name}")
    footer_line = f"_{footer_md}_"
    if summary_md:
        candidate = f"{header}\n\n{summary_md}\n\n{footer_line}"
    else:
        candidate = f"{header}\n\n{footer_line}"
    if len(candidate) > TELEGRAM_MAX_CAPTION:
        overhead = len(header) + len(footer_line) + 4
        limit_for_summary = max(0, TELEGRAM_MAX_CAPTION - overhead)
        summary_md = safe_truncate_mdv2(summary_md, limit_for_summary)
        candidate = f"{header}\n\n{summary_md}\n\n{footer_line}" if summary_md else f"{header}\n\n{footer_line}"
    return candidate


def unique_keys_for_entry(link: str, guid: Optional[str]) -> Tuple[str, ...]:
    keys = [f"link:{normalize_url(link)}"]
    if guid:
        keys.append(f"guid:{guid}")
    return tuple(keys)


async def send_news(session, title, link, source, posted_keys, image_cache, summary="", entry_image_url=None, dedup_keys=None) -> bool:
    dedup_keys = dedup_keys or (f"link:{normalize_url(link)}",)
    if any(k in posted_keys for k in dedup_keys):
        return False
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"{PRIMARY_BUTTON_PREFIX} {extract_domain(link)}", url=link))
    caption = build_caption(title, source, summary)
    image_url = None
    if entry_image_url:
        cand = urljoin(link, entry_image_url)
        if is_valid_image_url(cand):
            image_url = cand
    if not image_url:
        image_url = await extract_image_from_article(session, link, image_cache)
    try:
        if image_url:
            await bot.send_photo(CHANNEL_NAME, image_url, caption=caption, reply_markup=markup, parse_mode=TELEGRAM_PARSE_MODE)
        else:
            await bot.send_message(CHANNEL_NAME, caption, reply_markup=markup, disable_web_page_preview=False, parse_mode=TELEGRAM_PARSE_MODE)
        for k in dedup_keys:
            posted_keys.append(k)
        save_data(posted_keys, image_cache)
        logger.info(f"[PUBLISHED] {source}: {title}")
        return True
    except Exception as e:
        logger.error(f"[SEND ERROR primary] {link}: {e}")
        return False


async def check_one_source(session, source, url, posted_keys, image_cache) -> int:
    xml = await fetch(session, url)
    if not xml:
        return 0
    feed = feedparser.parse(xml)
    entries = getattr(feed, "entries", []) or []
    new_count = 0
    for entry in entries[:12]:
        title = getattr(entry, "title", None) or entry.get("title") or "Без названия"
        link = getattr(entry, "link", None) or entry.get("link") or ""
        if not link:
            continue
        summary = getattr(entry, "summary", None) or entry.get("summary") or getattr(entry, "description", None) or entry.get("description") or ""
        guid = getattr(entry, "id", None) or entry.get("id") or getattr(entry, "guid", None) or entry.get("guid")
        entry_image = select_image_from_entry(entry, link)
        success = await send_news(session, title, normalize_url(link), source, posted_keys, image_cache, summary, entry_image, unique_keys_for_entry(link, guid))
        if success:
            new_count += 1
        await asyncio.sleep(random.uniform(0.5, 1.2))
    return new_count


async def check_news_sources(posted_keys, image_cache) -> int:
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_one_source(session, s, u, posted_keys, image_cache) for s, u in RSS_FEEDS.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    total = 0
    for r in results:
        if isinstance(r, int):
            total += r
        else:
            logger.error(f"[RSS TASK ERROR] {r}")
    return total


stop_event = asyncio.Event()


def handle_shutdown():
    logger.info("🛑 Завершение работы...")
    stop_event.set()


async def news_monitor():
    posted_keys, image_cache = load_data()
    logger.info("🚀 Первая проверка...")
    await check_news_sources(posted_keys, image_cache)
    while not stop_event.is_set():
        try:
            await check_news_sources(posted_keys, image_cache)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            logger.error(f"‼️ Ошибка в основном цикле: {e}")
            await asyncio.sleep(60)


async def main():
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, handle_shutdown)
    loop.add_signal_handler(signal.SIGTERM, handle_shutdown)
    try:
        await news_monitor()
    except asyncio.CancelledError:
        pass
    finally:
        posted_keys, image_cache = load_data()
        save_data(posted_keys, image_cache)
        if hasattr(bot, "close_session"):
            await bot.close_session()


if __name__ == "__main__":
    asyncio.run(main())