"""
AnimeSchedule Discord Bot
OAuth2-authenticated access to animeschedule.net API v3.

RSS feeds are loaded from 'feeds.txt' (name|url per line).
Feeds are fetched live every 5 minutes.
Only entries from the last 24 hours are posted.
Images are extracted automatically when available.
"""

import asyncio
import calendar
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
import feedparser
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("anime-bot")

# ─── Environment variables ────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
AS_APP_TOKEN = os.environ["ANIMESCHEDULE_APP_TOKEN"]
AS_CLIENT_ID = os.environ["ANIMESCHEDULE_CLIENT_ID"]
AS_CLIENT_SECRET = os.environ["ANIMESCHEDULE_CLIENT_SECRET"]
OAUTH_REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback"
)
OAUTH_CALLBACK_PORT = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))

# ─── API constants ────────────────────────────────────────────────────────────
BASE_API = "https://animeschedule.net/api/v3"
BASE_SITE = "https://animeschedule.net"
IMAGE_BASE = "https://img.animeschedule.net/production/assets/public/img"

OAUTH_AUTHORIZE_URL = f"{BASE_API}/oauth2/authorize"
OAUTH_TOKEN_URL = f"{BASE_API}/oauth2/token"
OAUTH_REVOKE_URL = f"{BASE_API}/oauth2/revoke"

# Discord hard limits
EMBED_CHAR_LIMIT = 6000
FIELD_VALUE_LIMIT = 1024
EMBEDS_PER_MESSAGE = 5

# ─── RSS feeds file ───────────────────────────────────────────────────────────
FEEDS_FILE = Path("feeds.txt")

# Structure: {feed_name: feed_url}
RSS_FEEDS: dict[str, str] = {}


def load_feeds_from_file():
    """Read feeds.txt and populate RSS_FEEDS."""
    global RSS_FEEDS
    RSS_FEEDS.clear()
    if not FEEDS_FILE.exists():
        log.warning("feeds.txt not found – no RSS feeds available.")
        return
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" not in line:
                log.warning("feeds.txt line %d: missing '|', skipping", line_num)
                continue
            name, url = line.split("|", 1)
            name = name.strip()
            url = url.strip()
            if not name or not url:
                log.warning("feeds.txt line %d: empty name or URL, skipping", line_num)
                continue
            RSS_FEEDS[name] = url
    log.info("Loaded %d RSS feeds from feeds.txt", len(RSS_FEEDS))


# ─── Persistent data file ─────────────────────────────────────────────────────
DATA_FILE = Path("bot_data.json")

# ─── Persistent state (loaded from / saved to JSON) ─────────────────────────
feed_subscriptions: dict[
    int, dict[str, int]
] = {}  # guild_id -> {feed_name: channel_id}
seen_entries: dict[str, set] = {}  # feed_url -> set of entry guids
daily_channels: dict[int, int] = {}  # guild_id -> channel_id

# ─── Non‑persistent state ───────────────────────────────────────────────────
pending_oauth: dict[str, tuple[int, int]] = {}  # state -> (user_id, channel_id)
user_display_mode: dict[int, str] = {}  # user_id -> "list"/"visual"


class OAuthToken:
    def __init__(self, access_token: str, refresh_token: str, expires_at: float):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60


user_oauth_tokens: dict[int, OAuthToken] = {}

# ─── Persistent data helpers ─────────────────────────────────────────────────


def load_data():
    global feed_subscriptions, seen_entries, daily_channels
    if not DATA_FILE.exists():
        return
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        log.error("Failed to load data: %s", e)
        return

    feed_subscriptions = {
        int(g): v for g, v in data.get("feed_subscriptions", {}).items()
    }
    seen_entries = {k: set(v) for k, v in data.get("seen_entries", {}).items()}
    daily_channels = {int(g): v for g, v in data.get("daily_channels", {}).items()}


def save_data():
    data = {
        "feed_subscriptions": feed_subscriptions,
        "seen_entries": {k: list(v) for k, v in seen_entries.items()},
        "daily_channels": daily_channels,
    }
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error("Failed to save data: %s", e)


# ─── RSS helpers ────────────────────────────────────────────────────────────


def parse_entry_date(entry) -> datetime | None:
    """Convert feedparser's published_parsed to UTC datetime, or return None."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime.fromtimestamp(
            calendar.timegm(entry.published_parsed), tz=timezone.utc
        )
    return None


def extract_image_from_entry(entry) -> str | None:
    """
    Try to extract an image URL from an RSS entry.
    Checks: media:thumbnail, media:content, enclosure, itunes:image, and description.
    """
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        for thumb in entry.media_thumbnail:
            url = thumb.get("url")
            if url:
                return url

    if hasattr(entry, "media_content") and entry.media_content:
        for mc in entry.media_content:
            if mc.get("type", "").startswith("image"):
                url = mc.get("url")
                if url:
                    return url

    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image"):
                url = enc.get("href") or enc.get("url")
                if url:
                    return url

    if hasattr(entry, "itunes_image") and entry.itunes_image:
        return entry.itunes_image.get("href")

    if hasattr(entry, "description") and entry.description:
        match = re.search(r'<img[^>]+src="([^">]+)"', entry.description)
        if match:
            return match.group(1)

    return None


# ─── Colours ─────────────────────────────────────────────────────────────────
EMBED_COLOUR = discord.Colour.from_str("#7289DA")
WEEKDAY_COLOURS = (
    (discord.Colour.blue(), "Monday"),
    (discord.Colour.green(), "Tuesday"),
    (discord.Colour.gold(), "Wednesday"),
    (discord.Colour.orange(), "Thursday"),
    (discord.Colour.red(), "Friday"),
    (discord.Colour.purple(), "Saturday"),
    (discord.Colour.teal(), "Sunday"),
)

# ─── Bot setup ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ═══════════════════════════════════════════════════════════════════════════════
# OAuth2 helpers
# ═══════════════════════════════════════════════════════════════════════════════


def build_authorize_url(state: str, scope: str = "animelist") -> str:
    params = {
        "client_id": AS_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(
    session: aiohttp.ClientSession, code: str
) -> dict | None:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "client_id": AS_CLIENT_ID,
        "client_secret": AS_CLIENT_SECRET,
    }
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                log.error(
                    "Token exchange failed %s: %s", resp.status, await resp.text()
                )
                return None
            return await resp.json()
    except Exception as exc:
        log.error("Token exchange exception: %s", exc)
        return None


async def refresh_access_token(
    session: aiohttp.ClientSession, refresh_token: str
) -> dict | None:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": AS_CLIENT_ID,
        "client_secret": AS_CLIENT_SECRET,
    }
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                log.error("Token refresh failed %s: %s", resp.status, await resp.text())
                return None
            return await resp.json()
    except Exception as exc:
        log.error("Token refresh exception: %s", exc)
        return None


async def revoke_token(session: aiohttp.ClientSession, token: str) -> bool:
    data = {
        "token": token,
        "client_id": AS_CLIENT_ID,
        "client_secret": AS_CLIENT_SECRET,
    }
    try:
        async with session.post(
            OAUTH_REVOKE_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        log.error("Token revoke exception: %s", exc)
        return False


async def get_valid_access_token(
    session: aiohttp.ClientSession, discord_user_id: int
) -> str | None:
    tok = user_oauth_tokens.get(discord_user_id)
    if tok is None:
        return None
    if tok.is_expired():
        result = await refresh_access_token(session, tok.refresh_token)
        if not result:
            del user_oauth_tokens[discord_user_id]
            return None
        expires_at = time.time() + result.get("expires_in", 3600)
        user_oauth_tokens[discord_user_id] = OAuthToken(
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token", tok.refresh_token),
            expires_at=expires_at,
        )
        return result["access_token"]
    return tok.access_token


# ═══════════════════════════════════════════════════════════════════════════════
# aiohttp OAuth2 callback server
# ═══════════════════════════════════════════════════════════════════════════════


async def oauth_callback_handler(request: web.Request) -> web.Response:
    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    if error:
        return web.Response(text="❌ Authorization denied. You can close this tab.")

    if not code or not state:
        return web.Response(text="❌ Missing code or state.", status=400)

    entry = pending_oauth.pop(state, None)
    if entry is None:
        return web.Response(
            text="❌ Unknown or expired state. Run !login again.", status=400
        )

    discord_user_id, discord_channel_id = entry

    async with aiohttp.ClientSession() as session:
        result = await exchange_code_for_token(session, code)

    if not result:
        return web.Response(
            text="❌ Token exchange failed. Please try again.", status=500
        )

    expires_at = time.time() + result.get("expires_in", 3600)
    user_oauth_tokens[discord_user_id] = OAuthToken(
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token", ""),
        expires_at=expires_at,
    )
    log.info("OAuth2 token stored for Discord user %s", discord_user_id)

    channel = bot.get_channel(discord_channel_id)
    if channel:
        try:
            user = await bot.fetch_user(discord_user_id)
            embed = discord.Embed(
                title="✅ AnimeSchedule Authorization Successful",
                description=(
                    f"**{user.mention}** is now connected to AnimeSchedule.net!\n\n"
                    "You can now use `!animelist` and other OAuth2 commands.\n"
                    "Your token auto-refreshes as needed."
                ),
                colour=discord.Colour.green(),
            )
            await channel.send(embed=embed)
        except Exception as exc:
            log.error("Failed to notify Discord after OAuth: %s", exc)

    return web.Response(text="✅ Authorization successful! You can close this tab.")


async def start_oauth_server():
    app = web.Application()
    app.router.add_get("/oauth/callback", oauth_callback_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", OAUTH_CALLBACK_PORT).start()
    log.info("OAuth2 callback server listening on port %s", OAUTH_CALLBACK_PORT)


# ═══════════════════════════════════════════════════════════════════════════════
# AnimeSchedule API helpers
# ═══════════════════════════════════════════════════════════════════════════════


def app_headers() -> dict:
    return {
        "Authorization": f"Bearer {AS_APP_TOKEN}",
        "Content-Type": "application/json",
    }


def oauth_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


async def fetch_timetable(
    session: aiohttp.ClientSession, air_type: str = "sub"
) -> list:
    url = f"{BASE_API}/timetables/{air_type}"
    async with session.get(url, headers=app_headers()) as resp:
        if resp.status == 401:
            log.error("Timetable 401 — check ANIMESCHEDULE_APP_TOKEN")
            return []
        if resp.status != 200:
            log.error("Timetable fetch HTTP %s", resp.status)
            return []
        data = await resp.json()
        return data if isinstance(data, list) else []


async def fetch_animelist(session: aiohttp.ClientSession, access_token: str) -> list:
    url = f"{BASE_API}/animelists"
    async with session.get(url, headers=oauth_headers(access_token)) as resp:
        if resp.status == 401:
            log.warning("Animelist 401 — OAuth2 token expired?")
            return []
        if resp.status != 200:
            log.error("Animelist fetch HTTP %s", resp.status)
            return []
        data = await resp.json()
        return data if isinstance(data, list) else []


async def fetch_show_detail(session: aiohttp.ClientSession, route: str) -> dict | None:
    url = f"{BASE_API}/shows/{route}"
    try:
        async with session.get(url, headers=app_headers()) as resp:
            if resp.status == 404:
                log.warning("Show not found: %s", route)
                return None
            if resp.status != 200:
                log.error("Show detail fetch HTTP %s for %s", resp.status, route)
                return None
            return await resp.json()
    except Exception as exc:
        log.error("Show detail fetch exception for %s: %s", route, exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════════════════


def get_show_image(image_version_route: str) -> str:
    if image_version_route:
        return f"{IMAGE_BASE}/{image_version_route}"
    return ""


def get_show_url(route: str) -> str:
    return f"{BASE_SITE}/shows/{route}"


def parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(date_str)
        return None if dt.year == 1 else dt
    except (ValueError, TypeError):
        return None


def fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%H:%M UTC") if dt else "TBA"


def filter_by_date(shows: list, target: datetime.date) -> list:
    filtered_shows = []
    for show in shows:
        episode_date_str = show.get("episodeDate")
        if episode_date_str:
            try:
                episode_date = datetime.fromisoformat(episode_date_str)
                if episode_date.date() == target:
                    filtered_shows.append(show)
            except ValueError:
                continue
    return filtered_shows


def group_by_weekday(shows: list) -> dict[str, list]:
    order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    groups = {d: [] for d in order}
    for show in sorted(
        shows,
        key=lambda s: (
            parse_date(s.get("episodeDate"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
    ):
        ep_date = parse_date(show.get("episodeDate"))
        day_name = ep_date.strftime("%A") if ep_date else "Unknown"
        if day_name in groups:
            groups[day_name].append(show)
    return groups


def embed_char_count(embed: discord.Embed) -> int:
    total = len(embed.title or "")
    total += len(embed.description or "")
    for field in embed.fields:
        total += len(field.name or "")
        total += len(field.value or "")
    if embed.footer:
        total += len(embed.footer.text or "")
    if embed.author:
        total += len(embed.author.name or "")
    return total


# ═══════════════════════════════════════════════════════════════════════════════
# Embed builders
# ═══════════════════════════════════════════════════════════════════════════════


def make_visual_embed(
    timetable_show: dict,
    detail: dict | None,
    colour: discord.Colour = EMBED_COLOUR,
) -> discord.Embed:
    """
    Build a full-detail embed from:
      - timetable_show  (TimetableShow, PascalCase fields from /api/v3/timetables)
      - detail          (ShowDetail, PascalCase fields from /api/v3/shows/{route}, may be None)
    """
    # Titles
    names = (detail or {}).get("names") or {}
    title_main = (
        timetable_show.get("title")
        or names.get("english")
        or names.get("romaji")
        or names.get("japanese")
        or "Unknown Title"
    )
    title_en = names.get("english") or timetable_show.get("english") or ""
    title_ro = names.get("romaji") or timetable_show.get("romaji") or ""
    title_jp = names.get("japanese") or timetable_show.get("japanese") or ""
    route = timetable_show.get("route", "")

    # Image
    img_ver = (
        (detail or {}).get("ImageVersionRoute")
        or timetable_show.get("imageVersionRoute")
        or ""
    )
    image_url = get_show_image(img_ver)

    # Synopsis
    synopsis_raw = (detail or {}).get("Description") or ""
    synopsis = synopsis_raw[:400] + ("…" if len(synopsis_raw) > 400 else "")

    # Episode info
    ep_date = parse_date(timetable_show.get("episodeDate"))
    ep_num = timetable_show.get("episodeNumber")
    ep_total = timetable_show.get("episodes") or (detail or {}).get("episodes")
    air_type = timetable_show.get("airType", "")
    length = timetable_show.get("lengthMin") or (detail or {}).get("lengthMin")

    # Status
    status = (
        (detail or {}).get("status")
        or timetable_show.get("airingStatus")
        or timetable_show.get("status")
        or ""
    )

    # Release date / season
    premier_raw = (
        (detail or {}).get("premier") or (detail or {}).get("subPremier") or ""
    )
    premier_dt = parse_date(premier_raw)
    release_str = premier_dt.strftime("%B %d, %Y") if premier_dt else ""

    season_obj = (detail or {}).get("season") or {}
    season_name = season_obj.get("season") or ""
    season_year = season_obj.get("year") or str((detail or {}).get("year") or "")
    season_str = (
        f"{season_name} {season_year}".strip() if season_name or season_year else ""
    )

    # Metadata
    def keyword_names(lst, limit=6):
        if not isinstance(lst, list):
            return ""
        return ", ".join(k.get("name", "") for k in lst[:limit] if k.get("name"))

    genres_str = keyword_names((detail or {}).get("genres"))
    studios_str = keyword_names((detail or {}).get("studios"), limit=3)
    sources_str = keyword_names((detail or {}).get("sources"), limit=3)
    media_str = keyword_names((detail or {}).get("mediaTypes"), limit=3)

    air_labels = {"raw": "Raw 🇯🇵", "sub": "Subbed 📝", "dub": "Dubbed 🔊"}

    # Build embed
    embed = discord.Embed(
        title=title_main,
        url=get_show_url(route),
        description=synopsis if synopsis else None,
        colour=colour,
    )

    if image_url:
        embed.set_image(url=image_url)

    # Alternate titles
    if title_ro and title_ro != title_main:
        embed.add_field(name="🔤 Romaji", value=title_ro, inline=False)
    if title_jp and title_jp != title_main:
        embed.add_field(name="🇯🇵 Japanese", value=title_jp, inline=False)
    if title_en and title_en != title_main:
        embed.add_field(name="🌐 English", value=title_en, inline=False)

    # Air / episode details
    if ep_date:
        embed.add_field(
            name="🗓️ Ep Day", value=WEEKDAY_COLOURS[ep_date.weekday()][1], inline=True
        )
        embed.color = WEEKDAY_COLOURS[ep_date.weekday()][0]
        embed.add_field(name="🕐 Air Time", value=fmt_time(ep_date), inline=True)
    if ep_num:
        ep_str = f"{ep_num}" + (f" / {ep_total}" if ep_total else "")
        embed.add_field(name="📺 Episode", value=ep_str, inline=True)
    if length:
        embed.add_field(name="⏱️ Ep. Length", value=f"{length} min", inline=True)
    if air_type:
        embed.add_field(
            name="📡 Type",
            value=air_labels.get(air_type, air_type.upper()),
            inline=True,
        )
    if status:
        embed.add_field(name="📊 Status", value=status, inline=True)

    # Production / release
    if release_str:
        embed.add_field(name="📅 Release Date", value=release_str, inline=True)
    if season_str:
        embed.add_field(name="🗓️ Season", value=season_str, inline=True)
    if studios_str:
        embed.add_field(name="🎬 Studio", value=studios_str, inline=True)
    if sources_str:
        embed.add_field(name="📖 Source", value=sources_str, inline=True)
    if media_str:
        embed.add_field(name="🎭 Media Type", value=media_str, inline=True)
    if genres_str:
        embed.add_field(name="🏷️ Genres", value=genres_str, inline=False)

    embed.set_footer(text="AnimeSchedule.net")
    return embed


def _make_empty_list_embed(title: str, colour: discord.Colour) -> discord.Embed:
    embed = discord.Embed(title=title, colour=colour)
    embed.set_footer(text="AnimeSchedule.net")
    return embed


def make_list_embeds(
    shows: list,
    section_title: str,
    colour: discord.Colour = EMBED_COLOUR,
    shows_per_embed: int = 15,
) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    current = _make_empty_list_embed(section_title, colour)
    show_count = 0

    for show in shows:
        title = show.get("title") or show.get("romaji") or "Unknown"
        ep_d = parse_date(show.get("episodeDate"))
        ep_num = show.get("episodeNumber", "?")
        route = show.get("route", "")

        field_name = f"{title}"
        field_value = f"[Episode {ep_num} · {fmt_time(ep_d)}]({get_show_url(route)})"

        added_chars = len(field_name) + len(field_value)

        if (
            embed_char_count(current) + added_chars > EMBED_CHAR_LIMIT - 100
            or len(current.fields) >= 25
        ):
            embeds.append(current)
            current = _make_empty_list_embed(f"{section_title} (cont.)", colour)

        current.add_field(name=field_name, value=field_value, inline=False)
        show_count += 1

    if len(current.fields) > 0:
        embeds.append(current)

    if embeds:
        last = embeds[-1]
        last.set_footer(text=f"AnimeSchedule.net · {show_count} releases")

    return embeds


def make_week_list_embeds(
    groups: dict[str, list],
    today: datetime.date,
) -> list[discord.Embed]:
    weekdays = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    all_embeds: list[discord.Embed] = []

    for i, day_name in enumerate(weekdays):
        day_shows = groups.get(day_name, [])
        if not day_shows:
            continue

        ep_d = parse_date(day_shows[0].get("episodeDate"))
        date_str = ep_d.strftime("%B %d") if ep_d else ""
        is_today = ep_d and ep_d.date() == today
        label = f"{'📌 ' if is_today else ''}📅 {day_name} {date_str}"
        colour = WEEKDAY_COLOURS[i % len(WEEKDAY_COLOURS)][0]

        day_embeds = make_list_embeds(day_shows, label, colour)
        all_embeds.extend(day_embeds)

    return all_embeds


def make_rss_embed(
    entry: dict, feed_name: str, image_url: str | None = None
) -> discord.Embed:
    title = entry.get("title", "New Release")
    link = entry.get("link", "")
    summary = (entry.get("summary") or "")[:350]

    embed = discord.Embed(
        title=title,
        url=link if link else None,
        description=summary if summary else None,
        colour=EMBED_COLOUR,
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_author(name=f"New Release · {feed_name}")
    embed.set_footer(text="AnimeSchedule.net")
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
# Sending helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def send_embeds(ctx, embeds: list[discord.Embed], header: str | None = None):
    if not embeds:
        await ctx.send("📭 No releases found.")
        return
    if header:
        await ctx.send(header)
    for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        await ctx.send(embeds=embeds[i : i + EMBEDS_PER_MESSAGE])


async def send_visual(
    ctx, shows: list, header: str | None = None, colour: discord.Colour = EMBED_COLOUR
):
    if not shows:
        await ctx.send("📭 No releases found.")
        return
    if header:
        await ctx.send(header)

    async with aiohttp.ClientSession() as session:
        for i, show in enumerate(shows):
            route = show.get("route", "")
            detail = await fetch_show_detail(session, route) if route else None

            c = (
                WEEKDAY_COLOURS[i % len(WEEKDAY_COLOURS)][0]
                if len(shows) > 1
                else colour
            )
            embed = make_visual_embed(show, detail, c)
            await ctx.send(embed=embed)
            if (i + 1) % 5 == 0:
                await asyncio.sleep(1.5)


def get_mode(ctx: commands.Context, explicit: str) -> str:
    if explicit in ("list", "visual"):
        return explicit
    return user_display_mode.get(ctx.author.id, "list")


# ═══════════════════════════════════════════════════════════════════════════════
# Discord commands
# ═══════════════════════════════════════════════════════════════════════════════


@bot.command(name="mode")
async def cmd_mode(ctx: commands.Context, mode: str = ""):
    mode = mode.lower()
    if mode not in ("list", "visual", ""):
        await ctx.send("❌ Valid modes: `list` or `visual`")
        return

    if mode == "":
        current = user_display_mode.get(ctx.author.id, "list")
        await ctx.send(
            f"ℹ️ Your current display mode is **{current}**. Change it with `!mode list` or `!mode visual`."
        )
        return

    user_display_mode[ctx.author.id] = mode
    icon = "📋" if mode == "list" else "🖼️"
    desc = (
        "Compact summary — multiple shows per embed."
        if mode == "list"
        else "Rich card per show — portrait image, genres, studio, synopsis & more."
    )
    embed = discord.Embed(
        title=f"{icon} Display mode set to **{mode}**",
        description=desc,
        colour=EMBED_COLOUR,
    )
    embed.set_footer(text="This preference applies to !today, !tomorrow, and !week")
    await ctx.send(embed=embed)


@bot.command(name="today")
async def cmd_today(ctx: commands.Context, mode: str = ""):
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            shows = await fetch_timetable(session, "sub")

            today = datetime.now(timezone.utc).date()
            todays = filter_by_date(shows, today)
            display = get_mode(ctx, mode.lower())
            header = f"📅 **Anime Today — {today.strftime('%A, %B %d %Y')}** ({len(todays)} releases)"

            if not todays:
                await ctx.send(
                    "📭 No anime scheduled for today (or the schedule is unavailable)."
                )
                return

            if display == "visual":
                await send_visual(ctx, todays, header=header)
            else:
                embeds = make_list_embeds(
                    todays,
                    f"📅 Anime Today — {today.strftime('%A, %B %d %Y')}",
                    EMBED_COLOUR,
                )
                await send_embeds(ctx, embeds, header=header)


@bot.command(name="tomorrow")
async def cmd_tomorrow(ctx: commands.Context, mode: str = ""):
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            shows = await fetch_timetable(session, "sub")

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    tomorrows = filter_by_date(shows, tomorrow)
    display = get_mode(ctx, mode.lower())
    header = f"📅 **Anime Tomorrow — {tomorrow.strftime('%A, %B %d %Y')}** ({len(tomorrows)} releases)"

    if not tomorrows:
        await ctx.send("📭 No anime scheduled for tomorrow.")
        return

    if display == "visual":
        await send_visual(ctx, tomorrows, header=header)
    else:
        embeds = make_list_embeds(
            tomorrows,
            f"📅 Anime Tomorrow — {tomorrow.strftime('%A, %B %d %Y')}",
            EMBED_COLOUR,
        )
        await send_embeds(ctx, embeds, header=header)


@bot.command(name="week")
async def cmd_week(ctx: commands.Context, mode: str = ""):
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            shows = await fetch_timetable(session, "sub")

    if not shows:
        await ctx.send("📭 Could not fetch the weekly schedule.")
        return

    groups = group_by_weekday(shows)
    today = datetime.now(timezone.utc).date()
    display = get_mode(ctx, mode.lower())
    header = f"📆 **Weekly Anime Schedule** — {len(shows)} total releases"

    if display == "visual":
        weekdays = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        ordered = []
        for day in weekdays:
            ordered.extend(groups.get(day, []))
        await ctx.send(
            f"{header}\n⚠️ Visual mode will send **{len(ordered)} embeds** — one per show. This may take a moment…"
        )
        await send_visual(ctx, ordered)
    else:
        embeds = make_week_list_embeds(groups, today)
        await send_embeds(ctx, embeds, header=header)


@bot.command(name="login")
async def cmd_login(ctx: commands.Context):
    state = secrets.token_urlsafe(32)
    pending_oauth[state] = (ctx.author.id, ctx.channel.id)
    auth_url = build_authorize_url(state, scope="animelist")

    embed = discord.Embed(
        title="🔐 AnimeSchedule Authorization",
        description=(
            "Click the link below to authorize this bot with your AnimeSchedule.net account.\n\n"
            f"[**Authorize with AnimeSchedule.net**]({auth_url})\n\n"
            "After you approve, you'll be redirected back and the bot will confirm here.\n"
            "This link is single-use — run `!login` again if it expires."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text="Token is stored in memory and auto-refreshed.")

    try:
        await ctx.author.send(embed=embed)
        await ctx.send("📬 Check your DMs for the authorization link!")
    except discord.Forbidden:
        await ctx.send(embed=embed)


@bot.command(name="logout")
async def cmd_logout(ctx: commands.Context):
    tok = user_oauth_tokens.get(ctx.author.id)
    if not tok:
        await ctx.send("ℹ️ You are not currently logged in.")
        return
    async with aiohttp.ClientSession() as session:
        ok = await revoke_token(session, tok.access_token)
    del user_oauth_tokens[ctx.author.id]
    if ok:
        await ctx.send("✅ Your AnimeSchedule token has been revoked.")
    else:
        await ctx.send("⚠️ Token removed locally (server revocation may have failed).")


@bot.command(name="authstatus")
async def cmd_authstatus(ctx: commands.Context):
    tok = user_oauth_tokens.get(ctx.author.id)
    if not tok:
        embed = discord.Embed(
            title="🔓 Not Authorized",
            description="Run `!login` to connect your AnimeSchedule account.",
            colour=discord.Colour.red(),
        )
    else:
        remaining = max(0, int(tok.expires_at - time.time()))
        mins, secs = divmod(remaining, 60)
        embed = discord.Embed(
            title="🔐 Authorized",
            description=(
                f"Connected to AnimeSchedule.net.\n\n"
                f"**Token expires in:** {mins}m {secs}s\n"
                f"**Auto-refresh:** Enabled"
            ),
            colour=discord.Colour.green(),
        )
    embed.set_footer(text="AnimeSchedule.net OAuth2")
    await ctx.send(embed=embed)


@bot.command(name="animelist")
async def cmd_animelist(ctx: commands.Context):
    async with aiohttp.ClientSession() as session:
        access_token = await get_valid_access_token(session, ctx.author.id)
        if not access_token:
            await ctx.send(
                "🔐 Run `!login` first to connect your AnimeSchedule account."
            )
            return
        entries = await fetch_animelist(session, access_token)

    if not entries:
        await ctx.send("📭 Your anime list is empty or could not be retrieved.")
        return

    all_embeds: list[discord.Embed] = []
    current = discord.Embed(
        title=f"📋 {ctx.author.display_name}'s Anime List",
        colour=EMBED_COLOUR,
    )

    for entry in entries:
        title = entry.get("title") or entry.get("route") or "Unknown"
        ep_seen = entry.get("EpisodesSeen") or entry.get("episodesSeen") or 0
        ep_total = entry.get("Episodes") or entry.get("episodes")
        note = (entry.get("Note") or entry.get("note") or "")[:80]
        ep_str = f"{ep_seen}/{ep_total}" if ep_total else str(ep_seen)
        value = f"Episodes: {ep_str}" + (f"\n_{note}_" if note else "")

        added = len(title) + len(value)
        if (
            embed_char_count(current) + added > EMBED_CHAR_LIMIT - 100
            or len(current.fields) >= 25
        ):
            all_embeds.append(current)
            current = discord.Embed(
                title=f"📋 {ctx.author.display_name}'s Anime List (cont.)",
                colour=EMBED_COLOUR,
            )

        current.add_field(name=title, value=value, inline=True)

    if len(current.fields) > 0:
        current.set_footer(text=f"{len(entries)} total entries · AnimeSchedule.net")
        all_embeds.append(current)

    await send_embeds(ctx, all_embeds)


@bot.command(name="feed")
async def cmd_feed(ctx: commands.Context, action: str = "", feed_name: str = ""):
    if not ctx.guild:
        await ctx.send("❌ This command must be used in a server.")
        return

    if not RSS_FEEDS:
        await ctx.send(
            "❌ No RSS feeds configured. The bot owner must create a `feeds.txt` file."
        )
        return

    guild_id = ctx.guild.id
    action = action.lower()

    if action in ("list", ""):
        subs = feed_subscriptions.get(guild_id, {})
        embed = discord.Embed(
            title="📡 RSS Feed Subscriptions",
            description=(
                "`!feed enable <name>` — subscribe this channel\n"
                "`!feed disable <name>` — unsubscribe\n\n"
                f"**Available feeds:**\n" + "\n".join(f"`{name}`" for name in RSS_FEEDS)
            ),
            colour=EMBED_COLOUR,
        )
        for name in RSS_FEEDS:
            sub_ch = subs.get(name)
            if sub_ch == ctx.channel.id:
                status = "✅ Subscribed (this channel)"
            elif sub_ch:
                status = f"🔗 Subscribed in <#{sub_ch}>"
            else:
                status = "❌ Not subscribed"
            embed.add_field(name=f"`{name}`", value=status, inline=False)
        await ctx.send(embed=embed)

    elif action == "enable":
        if feed_name not in RSS_FEEDS:
            await ctx.send(
                f"❌ Unknown feed name `{feed_name}`. Available: {', '.join(f'`{n}`' for n in RSS_FEEDS)}"
            )
            return
        feed_subscriptions.setdefault(guild_id, {})[feed_name] = ctx.channel.id
        save_data()
        await ctx.send(
            f"✅ {ctx.channel.mention} subscribed to **{feed_name}**.\n"
            "New entries from this feed (posted within the last 24h) will be delivered here every 5 minutes."
        )

    elif action == "disable":
        if feed_name not in RSS_FEEDS:
            await ctx.send(f"❌ Unknown feed name `{feed_name}`.")
            return
        subs = feed_subscriptions.get(guild_id, {})
        if feed_name in subs:
            del subs[feed_name]
            save_data()
            await ctx.send(f"🗑️ Unsubscribed from **{feed_name}**.")
        else:
            await ctx.send(f"ℹ️ Not subscribed to `{feed_name}`.")
    else:
        await ctx.send("❓ Usage: `!feed list | enable <name> | disable <name>`")


@bot.command(name="daily")
async def cmd_daily(ctx: commands.Context, action: str = ""):
    if not ctx.guild:
        await ctx.send("❌ This command must be used in a server.")
        return

    guild_id = ctx.guild.id
    action = action.lower()

    if action == "":
        chan_id = daily_channels.get(guild_id)
        if chan_id:
            channel = bot.get_channel(chan_id)
            status = f"✅ Daily announcements are enabled in {channel.mention if channel else 'a deleted channel'}."
        else:
            status = "❌ Daily announcements are disabled."
        embed = discord.Embed(
            title="📆 Daily Schedule Announcement",
            description=status
            + "\n\nUse `!daily enable` to set this channel, or `!daily disable` to turn off.",
            colour=EMBED_COLOUR,
        )
        await ctx.send(embed=embed)

    elif action == "enable":
        daily_channels[guild_id] = ctx.channel.id
        save_data()
        await ctx.send(f"✅ Daily schedule will be posted here every day at 00:00 UTC.")

    elif action == "disable":
        if guild_id in daily_channels:
            del daily_channels[guild_id]
            save_data()
            await ctx.send("🗑️ Daily announcements disabled.")
        else:
            await ctx.send("ℹ️ Daily announcements were not enabled.")

    else:
        await ctx.send("❓ Usage: `!daily [enable|disable]`")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    current_mode = user_display_mode.get(ctx.author.id, "list")
    embed = discord.Embed(
        title="🤖 AnimeSchedule Bot — Commands",
        description=f"Powered by [AnimeSchedule.net](https://animeschedule.net) · Your display mode: **{current_mode}**",
        colour=EMBED_COLOUR,
    )
    embed.add_field(name="📅 Schedule", value="━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(
        name="!today [list|visual]",
        value="Today's anime releases.\n`list` = compact, `visual` = rich card per show",
        inline=False,
    )
    embed.add_field(
        name="!tomorrow [list|visual]",
        value="Tomorrow's anime releases.",
        inline=False,
    )
    embed.add_field(
        name="!week [list|visual]",
        value="Full weekly schedule, split safely across multiple messages.",
        inline=False,
    )
    embed.add_field(name="🖼️ Display Mode", value="━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(
        name="!mode [list|visual]",
        value=(
            "Set your default display mode.\n"
            "**list** — compact multi-show embeds (default)\n"
            "**visual** — one rich embed per show with image, genres, studio, synopsis"
        ),
        inline=False,
    )
    embed.add_field(name="🔐 Account", value="━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(
        name="!login",
        value="Authorize your AnimeSchedule account via OAuth2",
        inline=True,
    )
    embed.add_field(name="!logout", value="Revoke your OAuth2 token", inline=True)
    embed.add_field(name="!authstatus", value="Check authorization status", inline=True)
    embed.add_field(
        name="!animelist", value="View your anime list (requires !login)", inline=False
    )
    embed.add_field(name="📡 RSS Feeds", value="━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(
        name="!feed list", value="Show available feeds and subscriptions", inline=True
    )
    embed.add_field(
        name="!feed enable <name>",
        value="Subscribe this channel to a feed",
        inline=True,
    )
    embed.add_field(
        name="!feed disable <name>", value="Unsubscribe from a feed", inline=True
    )
    embed.add_field(
        name="Feeds",
        value="Defined in `feeds.txt` (name|url per line).",
        inline=False,
    )
    embed.add_field(name="📆 Daily", value="━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(
        name="!daily [enable|disable]",
        value="Automatically post today's schedule at 00:00 UTC in this channel.",
        inline=False,
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# RSS polling background task
# ═══════════════════════════════════════════════════════════════════════════════


@tasks.loop(minutes=5)
async def poll_rss_feeds():
    """Check all enabled feeds for new entries and post those less than 24h old."""
    active: dict[str, list[int]] = {}
    for guild_id, subs in feed_subscriptions.items():
        for feed_name, channel_id in subs.items():
            active.setdefault(feed_name, []).append(channel_id)

    if not active:
        log.debug("RSS poll: no active subscriptions")
        return

    now = datetime.now(timezone.utc)
    something_new = False

    async with aiohttp.ClientSession() as session:
        for feed_name, channel_ids in active.items():
            url = RSS_FEEDS.get(feed_name)
            if not url:
                log.warning("Feed %s has no URL, skipping", feed_name)
                continue

            try:
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        log.warning("Feed %s returned HTTP %d", feed_name, resp.status)
                        continue
                    text = await resp.text()
                    feed = feedparser.parse(text)
            except Exception as exc:
                log.warning("Error fetching feed %s: %s", feed_name, exc)
                continue

            feed_key = url
            seen_entries.setdefault(feed_key, set())

            for entry in feed.entries:
                eid = entry.get("id") or entry.get("link") or entry.get("title")
                if not eid:
                    log.debug("Skipping entry without ID in %s", feed_name)
                    continue

                if eid in seen_entries[feed_key]:
                    continue

                seen_entries[feed_key].add(eid)
                something_new = True

                pub_date = parse_entry_date(entry)
                if pub_date is None:
                    log.debug("Entry %s has no valid date – skipping", eid)
                    continue

                age = now - pub_date
                if age > timedelta(hours=24):
                    log.debug("Entry %s is older than 24h (%s) – not posting", eid, age)
                    continue

                image_url = extract_image_from_entry(entry)
                embed = make_rss_embed(entry, feed_name, image_url)

                for channel_id in channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(embed=embed)
                            log.debug(
                                "Sent RSS %s (entry %s) to channel %s",
                                feed_name,
                                eid,
                                channel_id,
                            )
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            log.warning("RSS send error to %s: %s", channel_id, exc)

    if something_new:
        save_data()


@poll_rss_feeds.before_loop
async def before_poll():
    await bot.wait_until_ready()
    load_data()
    load_feeds_from_file()
    log.info("RSS poller ready. %d feeds available.", len(RSS_FEEDS))


# ═══════════════════════════════════════════════════════════════════════════════
# Daily announcement task
# ═══════════════════════════════════════════════════════════════════════════════


@tasks.loop(time=datetime_time(hour=0, minute=0, tzinfo=timezone.utc))
async def daily_announcement():
    log.info("Running daily announcement task")
    if not daily_channels:
        return

    async with aiohttp.ClientSession() as session:
        shows = await fetch_timetable(session, "sub")

    if not shows:
        log.warning("Daily announcement: no timetable data")
        return

    today = datetime.now(timezone.utc).date()
    todays_shows = filter_by_date(shows, today)

    if not todays_shows:
        log.info("Daily announcement: no shows today")
        return

    embeds = make_list_embeds(
        todays_shows,
        f"📅 **Daily Anime — {today.strftime('%A, %B %d %Y')}** ({len(todays_shows)} releases)",
        EMBED_COLOUR,
    )

    for guild_id, channel_id in list(daily_channels.items()):
        channel = bot.get_channel(channel_id)
        if not channel:
            del daily_channels[guild_id]
            save_data()
            continue

        try:
            for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
                await channel.send(embeds=embeds[i : i + EMBEDS_PER_MESSAGE])
            log.debug("Daily announcement sent to guild %s", guild_id)
        except Exception as e:
            log.error("Failed to send daily announcement to %s: %s", guild_id, e)


# ═══════════════════════════════════════════════════════════════════════════════
# Bot events
# ═══════════════════════════════════════════════════════════════════════════════


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    poll_rss_feeds.start()
    daily_announcement.start()


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument. Try `!help`.")
        return
    log.error("Command error in %s: %s", ctx.command, error)
    await ctx.send(f"❌ An error occurred: `{error}`")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


async def main():
    await start_oauth_server()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
