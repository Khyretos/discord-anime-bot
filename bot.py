"""
AnimeSchedule Discord Bot
OAuth2-authenticated access to animeschedule.net API v3.

OAuth2 Flow:
  - Application token (env var) → used for all non-OAuth2 endpoints (timetables, shows, etc.)
  - Per-user OAuth2 tokens      → used for OAuth2-only endpoints (animelists, etc.)

A lightweight aiohttp web server handles the OAuth2 callback on OAUTH_REDIRECT_URI.

Display modes:
  - list   (default) → compact summary embeds, multiple shows per embed, char-limit-safe
  - visual           → one rich embed per show with image, genres, studio, synopsis, etc.
"""

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
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
# Load environment variables from .env file
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
EMBED_CHAR_LIMIT = 6000  # total characters per embed
FIELD_VALUE_LIMIT = 1024  # per-field value limit
EMBEDS_PER_MESSAGE = 5  # max embeds per message() call

# ─── RSS feed registry ────────────────────────────────────────────────────────
RSS_FEEDS = {
    "japanese": {
        "label": "🇯🇵 Japanese Anime (Raw)",
        "url": "https://animeschedule.net/rss/japanese",
    },
    "sub": {"label": "📺 Anime (Subbed)", "url": "https://animeschedule.net/rss/sub"},
    "dub": {"label": "🔊 Anime (Dubbed)", "url": "https://animeschedule.net/rss/dub"},
    "chinese": {
        "label": "🇨🇳 Chinese Anime (Donghua)",
        "url": "https://animeschedule.net/rss/donghua",
    },
    "manga": {"label": "📚 Manga", "url": "https://animeschedule.net/rss/manga"},
    "manhwa": {"label": "📖 Manhwa", "url": "https://animeschedule.net/rss/manhwa"},
}

# ─── In-memory state ─────────────────────────────────────────────────────────
feed_subscriptions: dict[int, dict[str, int]] = {}
seen_entries: dict[str, set] = {k: set() for k in RSS_FEEDS}
pending_oauth: dict[str, tuple[int, int]] = {}

# Per-user display mode preference: discord_user_id -> "list" | "visual"
user_display_mode: dict[int, str] = {}


class OAuthToken:
    def __init__(self, access_token: str, refresh_token: str, expires_at: float):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60


user_oauth_tokens: dict[int, OAuthToken] = {}

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
    """GET /api/v3/timetables/{airType}  —  Application token"""
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
    """GET /api/v3/animelists  —  OAuth2 token required"""
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
    """
    GET /api/v3/shows/{route}  —  Application token
    Returns full ShowDetail with PascalCase fields:
    Names{Romaji,English,Japanese}, Description, Genres[], Studios[],
    Sources[], MediaTypes[], LengthMin, Premier, Season, Status, ImageVersionRoute
    """
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
    """
    Build the correct image URL.
    The API returns ImageVersionRoute (e.g. "some-show-name/abc123") which is
    different from Route. Images are served as .webp files.
    """
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
    """Rough character count matching Discord's 6000-char limit calculation."""
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

# ── Visual mode: one rich embed per show ──────────────────────────────────────


def make_visual_embed(
    timetable_show: dict,
    detail: dict | None,
    colour: discord.Colour = EMBED_COLOUR,
) -> discord.Embed:
    """
    Build a full-detail embed from:
      - timetable_show  (TimetableShow, PascalCase fields from /api/v3/timetables)
      - detail          (ShowDetail, PascalCase fields from /api/v3/shows/{route}, may be None)

    TimetableShow fields used: Title, Romaji, English, Japanese, Route,
        ImageVersionRoute, AirType, AiringStatus, EpisodeDate, EpisodeNumber,
        Episodes, LengthMin, Status
    ShowDetail fields used: Names{Romaji,English,Japanese}, Description,
        Genres[]{Name}, Studios[]{Name}, Sources[]{Name}, MediaTypes[]{Name},
        LengthMin, Premier, Season{Season,Year}, Status, ImageVersionRoute
    """
    # ── Titles ────────────────────────────────────────────────────────────────
    # Prefer ShowDetail Names over timetable flat fields
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

    # ── Image ─────────────────────────────────────────────────────────────────
    # ImageVersionRoute is a versioned path like "my-show/a1b2c3", distinct from Route.
    img_ver = (
        (detail or {}).get("ImageVersionRoute")
        or timetable_show.get("imageVersionRoute")
        or ""
    )

    image_url = get_show_image(img_ver)

    # ── Synopsis ──────────────────────────────────────────────────────────────
    synopsis_raw = (detail or {}).get("Description") or ""
    synopsis = synopsis_raw[:400] + ("…" if len(synopsis_raw) > 400 else "")

    # ── Episode / air info from timetable ────────────────────────────────────
    ep_date = parse_date(timetable_show.get("episodeDate"))
    ep_num = timetable_show.get("episodeNumber")
    ep_total = timetable_show.get("episodes") or (detail or {}).get("episodes")
    air_type = timetable_show.get("airType", "")
    length = timetable_show.get("lengthMin") or (detail or {}).get("lengthMin")

    # ── Status / airing ───────────────────────────────────────────────────────
    status = (
        (detail or {}).get("status")
        or timetable_show.get("airingStatus")
        or timetable_show.get("status")
        or ""
    )

    # ── Release date / season ─────────────────────────────────────────────────
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

    # ── Metadata from ShowDetail ───────────────────────────────────────────────
    def keyword_names(lst, limit=6):
        if not isinstance(lst, list):
            return ""
        return ", ".join(k.get("name", "") for k in lst[:limit] if k.get("name"))

    genres_str = keyword_names((detail or {}).get("genres"))
    studios_str = keyword_names((detail or {}).get("studios"), limit=3)
    sources_str = keyword_names((detail or {}).get("sources"), limit=3)
    media_str = keyword_names((detail or {}).get("mediaTypes"), limit=3)

    air_labels = {"raw": "Raw 🇯🇵", "sub": "Subbed 📝", "dub": "Dubbed 🔊"}

    # ── Build embed ───────────────────────────────────────────────────────────
    embed = discord.Embed(
        title=title_main,
        url=get_show_url(route),
        description=synopsis if synopsis else None,
        colour=colour,
    )

    # Portrait image — this is what was broken before: wrong field name + wrong extension
    if image_url:
        embed.set_image(url=image_url)

    # ── Alternate titles ──────────────────────────────────────────────────────
    if title_ro and title_ro != title_main:
        embed.add_field(name="🔤 Romaji", value=title_ro, inline=False)
    if title_jp and title_jp != title_main:
        embed.add_field(name="🇯🇵 Japanese", value=title_jp, inline=False)
    if title_en and title_en != title_main:
        embed.add_field(name="🌐 English", value=title_en, inline=False)

    # ── Air / episode details ─────────────────────────────────────────────────
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

    # ── Production / release ─────────────────────────────────────────────────
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


# ── List mode: compact summary embeds, multiple shows per embed ───────────────


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
    """
    Pack shows into compact list embeds that never exceed Discord's 6000-char limit.
    Each field = one show line.  We check the running char count before adding each
    field and open a new embed if adding it would breach the limit.
    """
    embeds: list[discord.Embed] = []
    current = _make_empty_list_embed(section_title, colour)
    show_count = 0

    for show in shows:
        # API returns PascalCase: Title, Romaji, EpisodeDate, EpisodeNumber, Route
        title = show.get("title") or show.get("romaji") or "Unknown"
        ep_d = parse_date(show.get("episodeDate"))
        ep_num = show.get("episodeNumber", "?")
        route = show.get("route", "")

        field_name = f"{title}"
        field_value = f"[Episode {ep_num} · {fmt_time(ep_d)}]({get_show_url(route)})"

        # Estimate chars this field would add
        added_chars = len(field_name) + len(field_value)

        # Check hard limits: 6000 total chars OR 25 fields per embed
        if (
            embed_char_count(current) + added_chars
            > EMBED_CHAR_LIMIT - 100  # 100-char safety buffer
            or len(current.fields) >= 25
        ):
            embeds.append(current)
            # Continuation embed — no title to save chars; use a compact header
            current = _make_empty_list_embed(f"{section_title} (cont.)", colour)

        current.add_field(name=field_name, value=field_value, inline=False)
        show_count += 1

    if len(current.fields) > 0:
        embeds.append(current)

    # Stamp final embed with total count
    if embeds:
        last = embeds[-1]
        last.set_footer(text=f"AnimeSchedule.net · {show_count} releases")

    return embeds


# ── Weekly list mode: one "section" per day, each section char-limit-safe ─────


def make_week_list_embeds(
    groups: dict[str, list],
    today: datetime.date,
) -> list[discord.Embed]:
    """Build all the week's list embeds, day by day, splitting where needed."""
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


# ── RSS embeds ────────────────────────────────────────────────────────────────


def make_rss_embed(entry: dict, feed_key: str) -> discord.Embed:
    title = entry.get("title", "New Release")
    link = entry.get("link", "")
    summary = (entry.get("summary") or "")[:350]

    image_url = None
    for thumb in entry.get("media_thumbnail", []):
        image_url = thumb.get("url")
        break
    if not image_url:
        for mc in entry.get("media_content", []):
            if (mc.get("type") or "").startswith("image"):
                image_url = mc.get("url")
                break

    colour_map = {
        "japanese": discord.Colour.red(),
        "sub": discord.Colour.blue(),
        "dub": discord.Colour.green(),
        "chinese": discord.Colour.gold(),
        "manga": discord.Colour.purple(),
        "manhwa": discord.Colour.teal(),
    }
    embed = discord.Embed(
        title=title,
        url=link if link else None,
        description=summary if summary else None,
        colour=colour_map.get(feed_key, EMBED_COLOUR),
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_author(name=f"New Release · {RSS_FEEDS[feed_key]['label']}")
    embed.set_footer(text="AnimeSchedule.net")
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
# Sending helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def send_embeds(ctx, embeds: list[discord.Embed], header: str | None = None):
    """Send any number of embeds, batching into messages of ≤10 each."""
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
    """
    Send one rich visual embed per show.
    Fetches ShowDetail for each show to get full info (genres, studios, synopsis, etc.).
    """
    if not shows:
        await ctx.send("📭 No releases found.")
        return
    if header:
        await ctx.send(header)

    async with aiohttp.ClientSession() as session:
        for i, show in enumerate(shows):
            route = show.get("route", "")
            # Fetch full ShowDetail for this show (has genres, studios, description, etc.)
            detail = await fetch_show_detail(session, route) if route else None

            c = (
                WEEKDAY_COLOURS[i % len(WEEKDAY_COLOURS)][0]
                if len(shows) > 1
                else colour
            )
            embed = make_visual_embed(show, detail, c)
            await ctx.send(embed=embed)
            # Avoid Discord rate limits on large batches
            if (i + 1) % 5 == 0:
                await asyncio.sleep(1.5)


def get_mode(ctx: commands.Context, explicit: str) -> str:
    """Resolve display mode: explicit arg > user preference > default 'list'."""
    if explicit in ("list", "visual"):
        return explicit
    return user_display_mode.get(ctx.author.id, "list")


# ═══════════════════════════════════════════════════════════════════════════════
# Discord commands
# ═══════════════════════════════════════════════════════════════════════════════


@bot.command(name="mode")
async def cmd_mode(ctx: commands.Context, mode: str = ""):
    """
    Set your preferred display mode for !today, !tomorrow, !week.

    Usage:
      !mode list    — compact summary (default)
      !mode visual  — one rich embed per show (image, genres, studio, etc.)
      !mode         — show current setting
    """
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
    """
    Show anime releases today.

    Usage:
      !today           — use your saved mode preference
      !today list      — compact list
      !today visual    — rich card per show
    """
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
    """
    Show anime releases tomorrow.

    Usage:
      !tomorrow           — use your saved mode preference
      !tomorrow list      — compact list
      !tomorrow visual    — rich card per show
    """
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
    """
    Show the full weekly schedule.

    Usage:
      !week           — use your saved mode preference
      !week list      — compact list, one embed per day (auto-splits if >6000 chars)
      !week visual    — rich card per show (sends many embeds — can be slow)
    """
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
        # Flatten all shows ordered by date
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
    """Start the OAuth2 authorization flow (DMs you the link)."""
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
    """Revoke your AnimeSchedule OAuth2 token."""
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
    """Check your OAuth2 authorization status."""
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
    """Show your AnimeSchedule anime list (requires !login)."""
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

    # Pack entries into char-limit-safe embeds
    all_embeds: list[discord.Embed] = []
    current = discord.Embed(
        title=f"📋 {ctx.author.display_name}'s Anime List",
        colour=EMBED_COLOUR,
    )

    for entry in entries:
        # animelists endpoint may return PascalCase or camelCase — handle both
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
async def cmd_feed(ctx: commands.Context, action: str = "", feed_key: str = ""):
    """
    Manage RSS feed subscriptions for this channel.

    Usage:
      !feed list              — show available feeds and current subscriptions
      !feed enable <key>      — subscribe this channel to a feed
      !feed disable <key>     — unsubscribe from a feed

    Keys: japanese · sub · dub · chinese · manga · manhwa
    """
    if not ctx.guild:
        await ctx.send("❌ This command must be used in a server.")
        return

    guild_id = ctx.guild.id
    action = action.lower()

    if action in ("list", ""):
        subs = feed_subscriptions.get(guild_id, {})
        embed = discord.Embed(
            title="📡 RSS Feed Subscriptions",
            description=(
                "`!feed enable <key>` — subscribe this channel\n"
                "`!feed disable <key>` — unsubscribe\n\n"
                "**Keys:** `japanese` · `sub` · `dub` · `chinese` · `manga` · `manhwa`"
            ),
            colour=EMBED_COLOUR,
        )
        for key, info in RSS_FEEDS.items():
            sub_ch = subs.get(key)
            if sub_ch == ctx.channel.id:
                status = "✅ Subscribed (this channel)"
            elif sub_ch:
                status = f"🔗 Subscribed in <#{sub_ch}>"
            else:
                status = "❌ Not subscribed"
            embed.add_field(
                name=f"`{key}` — {info['label']}", value=status, inline=False
            )
        await ctx.send(embed=embed)

    elif action == "enable":
        if feed_key not in RSS_FEEDS:
            await ctx.send(
                f"❌ Unknown key `{feed_key}`. Valid: {', '.join(f'`{k}`' for k in RSS_FEEDS)}"
            )
            return
        feed_subscriptions.setdefault(guild_id, {})[feed_key] = ctx.channel.id
        await ctx.send(
            f"✅ {ctx.channel.mention} subscribed to **{RSS_FEEDS[feed_key]['label']}**.\n"
            "New releases will be posted here every 5 minutes."
        )

    elif action == "disable":
        if feed_key not in RSS_FEEDS:
            await ctx.send(f"❌ Unknown key `{feed_key}`.")
            return
        subs = feed_subscriptions.get(guild_id, {})
        if feed_key in subs:
            del subs[feed_key]
            await ctx.send(f"🗑️ Unsubscribed from **{RSS_FEEDS[feed_key]['label']}**.")
        else:
            await ctx.send(f"ℹ️ Not subscribed to `{feed_key}`.")
    else:
        await ctx.send("❓ Usage: `!feed list | enable <key> | disable <key>`")


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
        name="!feed list", value="Show feeds and subscriptions", inline=True
    )
    embed.add_field(
        name="!feed enable <key>", value="Subscribe this channel to a feed", inline=True
    )
    embed.add_field(
        name="!feed disable <key>", value="Unsubscribe from a feed", inline=True
    )
    embed.add_field(
        name="Feed keys",
        value="`japanese` · `sub` · `dub` · `chinese` · `manga` · `manhwa`",
        inline=False,
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# RSS polling background task
# ═══════════════════════════════════════════════════════════════════════════════


@tasks.loop(minutes=5)
async def poll_rss_feeds():
    active: dict[str, list[int]] = {}
    for subs in feed_subscriptions.values():
        for feed_key, channel_id in subs.items():
            active.setdefault(feed_key, []).append(channel_id)

    if not active:
        return

    loop = asyncio.get_event_loop()
    for feed_key, channel_ids in active.items():
        try:
            feed = await loop.run_in_executor(
                None, feedparser.parse, RSS_FEEDS[feed_key]["url"]
            )
        except Exception as exc:
            log.warning("RSS fetch error for %s: %s", feed_key, exc)
            continue

        new_entries = []
        for entry in feed.entries:
            eid = entry.get("id") or entry.get("link") or entry.get("title")
            if eid and eid not in seen_entries[feed_key]:
                seen_entries[feed_key].add(eid)
                new_entries.append(entry)

        for entry in reversed(new_entries):
            embed = make_rss_embed(entry, feed_key)
            for channel_id in channel_ids:
                channel = bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(embed=embed)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("RSS send error to %s: %s", channel_id, exc)


@poll_rss_feeds.before_loop
async def before_poll():
    await bot.wait_until_ready()
    log.info("Seeding RSS seen-entries cache…")
    loop = asyncio.get_event_loop()
    for feed_key, info in RSS_FEEDS.items():
        try:
            feed = await loop.run_in_executor(None, feedparser.parse, info["url"])
            for entry in feed.entries:
                eid = entry.get("id") or entry.get("link") or entry.get("title")
                if eid:
                    seen_entries[feed_key].add(eid)
        except Exception as exc:
            log.warning("RSS seed error for %s: %s", feed_key, exc)
    log.info("RSS seed complete.")


# ═══════════════════════════════════════════════════════════════════════════════
# Bot events
# ═══════════════════════════════════════════════════════════════════════════════


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    poll_rss_feeds.start()


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
