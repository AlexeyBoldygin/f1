import os
import json
import asyncio
import logging
import datetime
import html
import re
from collections import deque
from contextlib import suppress
from typing import Deque, Dict, Optional, Tuple, List
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================
# 🔧 PIT WALL CONFIGURATION
# ==========================
PIT_CREW_TOKEN = "7829313238:AAENEfOqkYpKLuuq-VNw4tYUs2KNF9z3n3o"
TEAM_RADIO_CHANNEL = "@f1russsia_news"

TEAM_GARAGE = "team_data.json"
LINK_PADDOCK_SIZE = 1000
PIT_STOP_INTERVAL = 60  # sec
TG_MAX_MESSAGE = 1024
TG_STYLE = "MarkdownV2"
HEADLINE_FLAG_DEFAULT = "🏎️💨"
HEADLINE_FLAG_BREAKING = "🏎️🔥"
HEADLINE_FLAG_GRAND_PRIX = "🏁"
CHECKERED_BAR = "⬛⬜" * 12
ACTION_BUTTON = "Открыть на"
SNIPPET_LIMIT = 420
HTTP_MAX_RETRIES = 2
HTTP_RETRY_BASE_DELAY = 0.6

RACE_SCHEDULE: Dict[str, str] = {
    "F1News.ru": "https://www.f1news.ru/export/news.xml",
    "Championat": "https://www.championat.com/rss/news/auto/f1/",
    "Sport-Express": "https://www.sport-express.ru/services/materials/news/formula1/se/",
}

HTTP_GEARBOX = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

tele_bot = AsyncTeleBot(PIT_CREW_TOKEN, parse_mode=TG_STYLE)

# ==========================
# 📟 Telemetry & Logs
# ==========================
logging.basicConfig(
    filename="f1_telemetry.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d]: %(message)s",
)
logger = logging.getLogger(__name__)

class RaceConsoleFormatter(logging.Formatter):
    LEVEL_PREFIX = {
        logging.DEBUG: "🔧 DEBUG",
        logging.INFO: "🟢 GO",
        logging.WARNING: "⚠️ YELLOW",
        logging.ERROR: "🛑 RED",
        logging.CRITICAL: "🛑 RED",
    }

    def format(self, record: logging.LogRecord) -> str:
        prefix = self.LEVEL_PREFIX.get(record.levelno, "🏁")
        record.msg = f"[{prefix}] {record.msg}"
        return super().format(record)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(RaceConsoleFormatter("%(asctime)s %(levelname)-8s: %(message)s"))
logging.getLogger().addHandler(_console_handler)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "yclid", "fbclid", "_openstat", "utm_referrer", "ref", "from",
    "tgShare", "tgs",
}

# Hashtags (clickable): do NOT escape these to keep them functional
BASE_HASHTAGS: List[str] = ["#F1", "#Formula1"]
TEAM_HASHTAGS = {
    "ferrari": "#Ferrari",
    "феррари": "#Ferrari",
    "red bull": "#RedBull",
    "ред булл": "#RedBull",
    "mercedes": "#Mercedes",
    "мерседес": "#Mercedes",
    "mclaren": "#McLaren",
    "макларен": "#McLaren",
    "aston martin": "#AstonMartin",
    "астон мартин": "#AstonMartin",
    "williams": "#Williams",
    "уильямс": "#Williams",
    "alpine": "#Alpine",
    "альпин": "#Alpine",
    "haas": "#HaasF1",
    "хаас": "#HaasF1",
    "sauber": "#Sauber",
    "заубер": "#Sauber",
}
DRIVER_HASHTAGS = {
    "верстаппен": "#Verstappen",
    "leclerc": "#Leclerc",
    "леклер": "#Leclerc",
    "sainz": "#Sainz",
    "сайнс": "#Sainz",
    "hamilton": "#Hamilton",
    "хэмилтон": "#Hamilton",
    "russell": "#Russell",
    "расселл": "#Russell",
    "perez": "#Perez",
    "перес": "#Perez",
    "norris": "#Norris",
    "норрис": "#Norris",
    "piastri": "#Piastri",
    "пиастри": "#Piastri",
    "alonso": "#Alonso",
    "алонсо": "#Alonso",
    "stroll": "#Stroll",
    "стролл": "#Stroll",
    "ocon": "#Ocon",
    "окон": "#Ocon",
    "gasly": "#Gasly",
    "гасли": "#Gasly",
    "bottas": "#Bottas",
    "боттас": "#Bottas",
    "zhou": "#Zhou",
    "чжоу": "#Zhou",
    "tsunoda": "#Tsunoda",
    "цунода": "#Tsunoda",
}
GP_TOKENS = ["гран-при", "grand prix", " gp", "gp "]

# ==========================
# 🧰 Pit Crew Tools
# ==========================

def load_team_data() -> Tuple[Deque[str], Dict[str, str]]:
    try:
        if os.path.exists(TEAM_GARAGE):
            with open(TEAM_GARAGE, "r", encoding="utf-8") as garage:
                team_data = json.load(garage)
                published = deque(team_data.get("published", []), maxlen=LINK_PADDOCK_SIZE)
                photo_cache = dict(team_data.get("photo_cache", {}))
                return published, photo_cache
        return deque(maxlen=LINK_PADDOCK_SIZE), {}
    except Exception as e:
        logger.error(f"📦 Data load failure: {e}")
        return deque(maxlen=LINK_PADDOCK_SIZE), {}

def save_team_data(published: Deque[str], photo_cache: Dict[str, str]) -> None:
    try:
        temp_pit = f"{TEAM_GARAGE}.tmp"
        team_state = {
            "published": list(published),
            "photo_cache": photo_cache,
        }
        with open(temp_pit, "w", encoding="utf-8") as garage:
            json.dump(team_state, garage, ensure_ascii=False, indent=2)
        os.replace(temp_pit, TEAM_GARAGE)
    except Exception as e:
        logger.error(f"💾 Data save error: {e}")

def clean_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        qpairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        qpairs.sort()
        query = urlencode(qpairs, doseq=True)
        path = re.sub(r"/{2,}", "/", parts.path)
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url

def extract_team(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return "source"

def strip_html(content: Optional[str]) -> str:
    if not content:
        return ""
    try:
        soup = BeautifulSoup(content, "html.parser")
        clean_text = " ".join(soup.stripped_strings)
    except Exception:
        clean_text = re.sub(r"<[^>]+>", " ", content)
    clean_text = html.unescape(clean_text)
    return re.sub(r"\s+", " ", clean_text).strip()

_MD_ESCAPE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")

def escape_md(text: str) -> str:
    if not text:
        return ""
    return _MD_ESCAPE.sub(r"\\\1", text)

def trim_md(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    clipped = text[:max_len]
    if clipped.endswith("\\"):
        clipped = clipped[:-1]
    return clipped

def pit_wall_summary(text: str, target: int = SNIPPET_LIMIT) -> str:
    if len(text) <= target:
        return text
    break_points: List[int] = []
    for marker in (".", "!", "?", "…"):
        pos = text.rfind(marker, 0, target + 1)
        if pos != -1:
            break_points.append(pos)
    if break_points:
        cut = max(break_points) + 1
        return text[:cut].strip()
    space_pos = text.rfind(" ", 0, target + 1)
    if space_pos != -1 and space_pos > target * 0.6:
        return (text[:space_pos] + "…").strip()
    return (text[:target - 1] + "…").strip()

def race_time(dt: datetime.datetime) -> str:
    months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    return f"{dt.strftime('%H:%M')} · {dt.day} {months[dt.month - 1]}"

async def fetch_data(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            async with session.get(url, headers=HTTP_GEARBOX, timeout=HTTP_TIMEOUT) as response:
                if response.status != 200:
                    logger.warning(f"⚠️ HTTP {response.status} @ {url}")
                    if attempt < HTTP_MAX_RETRIES:
                        await asyncio.sleep(HTTP_RETRY_BASE_DELAY * (2 ** attempt))
                        continue
                    return None
                return await response.text()
        except Exception as e:
            logger.error(f"📡 Comms failure @ {url}: {e}")
            if attempt < HTTP_MAX_RETRIES:
                await asyncio.sleep(HTTP_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            return None
    return None

def find_media(entry, base_url: str) -> Optional[str]:
    with suppress(Exception):
        media = getattr(entry, "media_content", None) or entry.get("media_content")
        if media and isinstance(media, list):
            for item in media:
                url = (item or {}).get("url")
                if url:
                    return urljoin(base_url, url)
    with suppress(Exception):
        links = getattr(entry, "links", None) or entry.get("links")
        if links and isinstance(links, list):
            for link in links:
                if (link or {}).get("rel") == "enclosure" and "image" in ((link or {}).get("type") or ""):
                    href = (link or {}).get("href")
                    if href:
                        return urljoin(base_url, href)
    return None

async def find_photo(session: aiohttp.ClientSession, url: str, photo_cache: Dict[str, str]) -> Optional[str]:
    if url in photo_cache:
        return photo_cache[url]
    try:
        content = await fetch_data(session, url)
        if not content:
            return None
        soup = BeautifulSoup(content, "html.parser")
        candidates: List[str] = []
        # OpenGraph & Twitter
        for prop in ("og:image:secure_url", "og:image"):
            tag = soup.find("meta", property=prop)
            if tag and tag.get("content"):
                candidates.append(tag["content"])
        for name in ("twitter:image:src", "twitter:image"):
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                candidates.append(tag["content"])
        # First inline img as fallback
        if not candidates:
            for img in soup.find_all("img"):
                src = img.get("src") or ""
                if not src:
                    continue
                full = urljoin(url, src)
                if any(full.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                    candidates.append(full)
                    break
        image_url: Optional[str] = None
        for candidate in candidates:
            if candidate:
                image_url = urljoin(url, candidate)
                break
        if image_url:
            photo_cache[url] = image_url
            return image_url
    except Exception as e:
        logger.error(f"📸 Image error @ {url}: {e}")
    return None

# ==========================
# 🏁 Formatting & Captioning
# ==========================

def _choose_header_icon(title: str) -> str:
    t = title.lower()
    if any(token in t for token in ("срочно", "breaking")):
        return HEADLINE_FLAG_BREAKING
    if any(token in t for token in ("гран-при", "grand prix", " gp", "gp ")):
        return HEADLINE_FLAG_GRAND_PRIX
    return HEADLINE_FLAG_DEFAULT

def _unique_preserve_order(items: List[str]) -> List[str]:
    seen: set = set()
    result: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result

def build_hashtags(title: str, summary: str) -> str:
    text = f"{title} {strip_html(summary)}".lower()
    tags: List[str] = []
    tags.extend(BASE_HASHTAGS)
    for token, tag in TEAM_HASHTAGS.items():
        if token in text:
            tags.append(tag)
    for token, tag in DRIVER_HASHTAGS.items():
        if token in text:
            tags.append(tag)
    if any(token in text for token in GP_TOKENS):
        tags.append("#ГранПри")
    cleaned = _unique_preserve_order(tags)
    return " ".join(cleaned[:6])

def create_broadcast(title: str, team: str, details: str) -> str:
    # Header
    icon = _choose_header_icon(title)
    title_clean = escape_md(title)
    header = f"{icon} *{title_clean}*"

    # Summary
    details_clean = strip_html(details)
    details_trimmed = pit_wall_summary(details_clean, SNIPPET_LIMIT)
    details_md = escape_md(details_trimmed) if details_trimmed else ""

    # Hashtags (not escaped to keep clickable)
    hashtags = build_hashtags(title, details)

    # Footer
    timestamp = race_time(datetime.datetime.now())
    footer = escape_md(f"🕒 {timestamp} · {team}")
    footer_line = f"_{footer}_"

    # Assemble with checkered bar
    parts: List[str] = [header, CHECKERED_BAR]
    if details_md:
        parts.extend(["", details_md])
    if hashtags:
        parts.extend(["", hashtags])
    parts.extend(["", footer_line])
    message = "\n".join(parts)

    # Fit to Telegram caption limit
    if len(message) <= TG_MAX_MESSAGE:
        return message

    # Trim summary first
    if details_md:
        overhead = len("\n".join([header, CHECKERED_BAR, "", "", hashtags, "", footer_line]))
        limit_for_summary = max(0, TG_MAX_MESSAGE - overhead)
        if limit_for_summary > 0:
            details_md = trim_md(details_md, limit_for_summary)
        else:
            details_md = ""
        parts = [header, CHECKERED_BAR]
        if details_md:
            parts.extend(["", details_md])
        if hashtags:
            parts.extend(["", hashtags])
        parts.extend(["", footer_line])
        message = "\n".join(parts)
        if len(message) <= TG_MAX_MESSAGE:
            return message

    # Remove hashtags if still too long
    hashtags = ""
    parts = [header, CHECKERED_BAR]
    if details_md:
        parts.extend(["", details_md])
    parts.extend(["", footer_line])
    message = "\n".join(parts)
    if len(message) <= TG_MAX_MESSAGE:
        return message

    # Remove checkered bar as last resort
    parts = [header]
    if details_md:
        parts.extend(["", details_md])
    parts.extend(["", footer_line])
    message = "\n".join(parts)
    if len(message) <= TG_MAX_MESSAGE:
        return message

    # Hard trim summary if still exceeding
    if details_md:
        overhead_min = len("\n".join([header, "", "", footer_line]))
        limit_for_summary = max(0, TG_MAX_MESSAGE - overhead_min)
        details_md = trim_md(details_md, limit_for_summary)
        parts = [header]
        if details_md:
            parts.extend(["", details_md])
        parts.extend(["", footer_line])
        message = "\n".join(parts)
        return message[:TG_MAX_MESSAGE]

    return message[:TG_MAX_MESSAGE]

# ==========================
# 📣 Broadcasting
# ==========================

def valid_photo(url: Optional[str]) -> bool:
    return bool(url) and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))

async def broadcast_update(
    session: aiohttp.ClientSession,
    headline: str,
    link: str,
    source: str,
    published: Deque[str],
    photo_cache: Dict[str, str],
    details: str = "",
    media_url: Optional[str] = None,
) -> bool:
    team = extract_team(link)
    content_id = f"link:{clean_url(link)}"

    if content_id in published:
        return False

    controls = InlineKeyboardMarkup()
    controls.add(InlineKeyboardButton(f"{ACTION_BUTTON} {team}", url=link))

    message = create_broadcast(headline, source, details)

    photo = media_url and urljoin(link, media_url)
    if photo and not valid_photo(photo):
        photo = None
    if not photo:
        photo = await find_photo(session, link, photo_cache)
        if photo and not valid_photo(photo):
            photo = None

    try:
        if photo and valid_photo(photo):
            await tele_bot.send_photo(
                TEAM_RADIO_CHANNEL,
                photo,
                caption=message,
                reply_markup=controls,
            )
        else:
            await tele_bot.send_message(
                TEAM_RADIO_CHANNEL,
                message,
                reply_markup=controls,
                disable_web_page_preview=False,
            )
        logger.info(f"📻 On air! {source}: {headline}")
        published.append(content_id)
        return True
    except Exception as e:
        logger.error(f"📡 Broadcast failure @ {link}: {e}")
        # Fallback: try plain text
        try:
            raw = f"{HEADLINE_FLAG_DEFAULT} {headline}\n\n{strip_html(details)}\n\n🕒 {race_time(datetime.datetime.now())} · {source}"
            if photo and valid_photo(photo):
                await tele_bot.send_photo(
                    TEAM_RADIO_CHANNEL,
                    photo,
                    caption=raw,
                    reply_markup=controls,
                    parse_mode=None,
                )
            else:
                await tele_bot.send_message(
                    TEAM_RADIO_CHANNEL,
                    raw,
                    reply_markup=controls,
                    disable_web_page_preview=False,
                    parse_mode=None,
                )
            published.append(content_id)
            logger.info(f"📻 Fallback delivered: {source}")
            return True
        except Exception as e2:
            logger.error(f"📡 Fallback failed @ {link}: {e2}")
            return False

# ==========================
# 📰 Feed Processing
# ==========================

async def process_feed(
    session: aiohttp.ClientSession,
    team: str,
    feed: str,
    published: Deque[str],
    photo_cache: Dict[str, str],
) -> int:
    feed_data = await fetch_data(session, feed)
    if not feed_data:
        return 0

    parsed = feedparser.parse(feed_data)
    entries = getattr(parsed, "entries", []) or []
    if not isinstance(entries, list):
        entries = []

    new_updates = 0
    for update in entries[:12]:
        headline = getattr(update, "title", None) or update.get("title") or "Без названия"
        link = getattr(update, "link", None) or update.get("link") or ""
        if not link:
            continue
        details = (
            getattr(update, "summary", None)
            or update.get("summary", "")
            or getattr(update, "description", None)
            or ""
        )
        media = find_media(update, link)
        success = await broadcast_update(
            session,
            headline,
            clean_url(link),
            team,
            published,
            photo_cache,
            details,
            media,
        )
        if success:
            new_updates += 1
        await asyncio.sleep(0.7)  # soft throttle

    logger.info(f"🏁 {team} debrief: {new_updates}/{len(entries[:12])} updates")
    return new_updates

async def scan_all_feeds(published: Deque[str], photo_cache: Dict[str, str]) -> int:
    logger.info(f"🏁 Starting formation lap ({len(published)} stored updates)")
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            process_feed(session, team, feed, published, photo_cache)
            for team, feed in RACE_SCHEDULE.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total_updates = 0
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"‼️ Team radio failure: {result}")
        else:
            total_updates += int(result or 0)
    return total_updates

# ==========================
# 🏎️ Main Loop
# ==========================

async def race_strategy() -> None:
    published, photo_cache = load_team_data()
    logger.info("🚦 Lights out! First scan…")
    await scan_all_feeds(published, photo_cache)
    while True:
        try:
            lap_start = datetime.datetime.now()
            new_updates = await scan_all_feeds(published, photo_cache)
            lap_time = (datetime.datetime.now() - lap_start).total_seconds()
            logger.info(
                f"✅ Pit stop complete! Lap: {lap_time:.1f}s | Updates: {new_updates} | Garage size: {len(published)}"
            )
            save_team_data(published, photo_cache)
            # Small jitter to spread load
            jitter = 0.2 * (1 + (datetime.datetime.now().second % 3))
            await asyncio.sleep(PIT_STOP_INTERVAL + jitter)
        except Exception as e:
            logger.error(f"🔥 Engine failure: {e}")
            await asyncio.sleep(60)

async def main_race() -> None:
    print("\n" + CHECKERED_BAR)
    print("🏎💨 FORMULA 1 NEWS BOT - STARTING ENGINE")
    print(CHECKERED_BAR)
    try:
        await race_strategy()
    except asyncio.CancelledError:
        pass
    finally:
        published, photo_cache = load_team_data()
        save_team_data(published, photo_cache)

if __name__ == "__main__":
    try:
        asyncio.run(main_race())
    except KeyboardInterrupt:
        print("\n🛑 Emergency stop! Powering down…")