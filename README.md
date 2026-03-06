
# 🎌 AnimeSchedule Discord Bot

A Discord bot that delivers anime release schedules, rich show cards, and live RSS feed updates — powered by the [AnimeSchedule.net API v3](https://animeschedule.net/api/v3/documentation).

---

## ✨ Features at a Glance

- **Two display modes** — compact list view or rich visual cards, switchable per-command or as a persistent preference
- **Daily & weekly schedules** — `!today`, `!tomorrow`, and `!week` with automatic 6000-character limit splitting
- **Visual mode** — one full embed per show including portrait image, synopsis, romaji/japanese/english titles, genres, studio, source, season, release date, episode length, air type, and status
- **Live RSS feeds** — auto‑posts new releases to subscribed channels every 5 minutes; only entries from the last 24 hours are delivered; images are automatically extracted when available
- **Flexible feed configuration** — define your own feeds in `feeds.txt` (name|url per line), no coding required
- **OAuth2 authentication** — per-user authorization flow for private API endpoints such as your personal anime list
- **Docker-ready** — ships with a `Dockerfile` and `docker-compose.yml` for one-command deployment
- **Persistent storage** — subscriptions and seen entry IDs are saved in `bot_data.json`, surviving restarts

---

## 📋 Command Reference

### 📅 Schedule

| Command            | Description                                           |
| ------------------ | ----------------------------------------------------- |
| `!today`           | Today's anime releases in your preferred display mode |
| `!today list`      | Force compact list view for today                     |
| `!today visual`    | Force rich visual cards for today                     |
| `!tomorrow`        | Tomorrow's anime releases                             |
| `!tomorrow list`   | Force compact list view for tomorrow                  |
| `!tomorrow visual` | Force rich visual cards for tomorrow                  |
| `!week`            | Full weekly schedule, Mon–Sun                         |
| `!week list`       | Force compact list view for the week                  |
| `!week visual`     | Force rich visual cards for the week                  |

> All schedule commands fetch the **Subbed (sub)** timetable from AnimeSchedule.net by default. The `!week` command automatically splits the output across multiple messages if any day's embed would exceed Discord's 6000-character limit.

---

### 🖼️ Display Modes

| Command        | Description                            |
| -------------- | -------------------------------------- |
| `!mode`        | Show your current display mode setting |
| `!mode list`   | Set default to compact list view       |
| `!mode visual` | Set default to rich visual card view   |

**`list` mode** packs multiple shows into a single embed per day. Each entry shows the show title (linked to its AnimeSchedule page), episode number, and air time. Multiple embeds are created automatically if a day's shows exceed the 6000-character limit.

**`visual` mode** sends one embed per show containing:
examples:

| List | Visual|
| ------- | ------- |
| ![alt text](/images/image-4.png) | ![alt text](/images/image-3.png) |

| Field                    | Source                                           |
| ------------------------ | ------------------------------------------------ |
| 🖼️ Portrait image        | `ImageVersionRoute` from API (served as `.webp`) |
| Title (main embed title) | English or Romaji title                          |
| 🔤 Romaji name           | `ShowDetail.names.romaji`                        |
| 🇯🇵 Japanese name         | `ShowDetail.names.japanese`                      |
| 🌐 English name          | `ShowDetail.names.english`                       |
| 📝 Synopsis              | `ShowDetail.description` (up to 400 characters)  |
| 🕐 Air Time              | UTC air time from timetable                      |
| 🗓️ Air Day               | Weekday with colour-coded accent                 |
| 📺 Episode               | Episode number / total episodes                  |
| ⏱️ Episode Length        | Runtime in minutes                               |
| 📡 Type                  | Raw 🇯🇵 / Subbed 📝 / Dubbed 🔊                   |
| 📊 Status                | Airing status from ShowDetail                    |
| 📅 Release Date          | Première date (formatted)                        |
| 🗓️ Season                | Season name and year                             |
| 🎬 Studio                | Production studio(s)                             |
| 📖 Source                | Source material (manga, novel, etc.)             |
| 🎭 Media Type            | Media classification                             |
| 🏷️ Genres                | Up to 6 genre tags                               |

> Visual mode for `!week` fetches a **ShowDetail API call per show**, which means it can send many embeds. The bot warns you upfront with a count and paces delivery to avoid Discord rate limits.

---

### 📡 RSS Feeds

The bot can monitor any number of RSS feeds defined in a simple text file.  
Feeds are fetched live every 5 minutes, and only entries **published within the last 24 hours** are posted. Images are automatically extracted when present in the feed (media:thumbnail, enclosure, etc.).

#### Configuration

1. Create a file named `feeds.txt` in the same directory as `bot.py`.
2. Add one feed per line in the format:  
   `Display Name|https://example.com/feed.xml`  
   Example:

    ```text
    Japanese|https://animeschedule.net/rss/japanese
    Subbed|https://animeschedule.net/rss/sub
    Dubbed|https://animeschedule.net/rss/dub
    News|<https://www.animenewsnetwork.com/all/rss.xml?ann-edition>
    ```

3. Restart the bot (or it will reload on next poll cycle).
4. examples

| Japanese | Dub|
| ------- | ------- |
| ![alt text](/images/image.png) | ![alt text](/images/image-1.png) |

| Sub | news |
| ------- | ------- |
|  ![alt text](/images/image-2.png) | ![alt text](/images/image-1.png) |

#### Commands

| Command               | Description                                                          |
| --------------------- | -------------------------------------------------------------------- |
| `!feed list`          | Show all configured feeds and which ones this channel is subscribed to |
| `!feed enable <name>` | Subscribe the current channel to a feed (by display name)           |
| `!feed disable <name>`| Unsubscribe from a feed                                              |

Subscriptions are saved in `bot_data.json` and survive restarts.

---

### 🔐 Account & OAuth2

| Command       | Description                                                           |
| ------------- | --------------------------------------------------------------------- |
| `!login`      | Start the OAuth2 flow — DMs you an authorization link                 |
| `!logout`     | Revoke your OAuth2 token on AnimeSchedule and remove it locally       |
| `!authstatus` | Check whether you're authorized and how long until your token expires |
| `!animelist`  | View your personal AnimeSchedule anime list _(requires `!login`)_     |

### 📆 Daily Announcements

| Command               | Description                                                   |
| --------------------- | ------------------------------------------------------------- |
| `!daily enable`       | Enable daily schedule posts in this channel at 00:00 UTC      |
| `!daily disable`      | Disable daily announcements                                   |
| `!daily`              | Show current daily announcement status for this server        |

### ℹ️ General

| Command | Description                                     |
| ------- | ----------------------------------------------- |
| `!help` | Show all commands and your current display mode |

## 🔐 Authentication Architecture

The bot uses **two distinct authentication layers**:

### 1. Application Token — Standard Endpoints

A static Bearer token tied to your registered AnimeSchedule application. Used for all non-personal endpoints:

- `GET /api/v3/timetables/{airType}` — weekly schedule
- `GET /api/v3/shows/{route}` — full show detail (genres, studios, synopsis, etc.)

Set via the `ANIMESCHEDULE_APP_TOKEN` environment variable.

### 2. OAuth2 User Tokens — Private Endpoints

A per-user access + refresh token obtained via the Authorization Code flow. Required for:

- `GET /api/v3/animelists` — personal anime list
- Any other endpoint requiring user identity

**Flow:**

```
User runs !login
    → Bot generates a state-secured authorization URL
    → User clicks the link in their DMs
    → AnimeSchedule shows the permission screen
    → User approves → redirected to OAUTH_REDIRECT_URI/oauth/callback
    → Bot's built-in aiohttp server receives the callback
    → Code is exchanged for access + refresh tokens
    → Bot confirms in Discord with a success embed
```

Tokens expire after **3600 seconds** (1 hour) per the API spec. The bot automatically refreshes them using the refresh token before any protected API call. If a refresh fails the user is asked to run `!login` again.

The built-in OAuth2 callback server runs on `OAUTH_CALLBACK_PORT` (default: `8080`) alongside the bot using `aiohttp`.

## 🚀 Setup

### Step 1 — Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. **New Application → Bot** — copy the **Token**
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. **OAuth2 → URL Generator** → scopes: `bot` → permissions: `Send Messages`, `Embed Links`, `Read Message History`
5. Use the generated URL to invite the bot to your server

### Step 2 — Get AnimeSchedule Credentials

Go to `https://animeschedule.net/users/<your_username>/settings/api`:

1. Copy your **Application Token** → `ANIMESCHEDULE_APP_TOKEN`
2. Create an **OAuth2 Application**:
   - Set the redirect URI to your `OAUTH_REDIRECT_URI` (e.g. `http://localhost:8080/oauth/callback`)
   - Copy the **Client ID** → `ANIMESCHEDULE_CLIENT_ID`
   - Copy the **Client Secret** → `ANIMESCHEDULE_CLIENT_SECRET`

### Step 3 — Configure Environment Variables

```bash
cp .env.example .env
# Edit .env and fill in all values
```

`.env.example`:

```env
# Discord bot token (required)
DISCORD_TOKEN=your_discord_bot_token_here

# AnimeSchedule application token — for timetables and show data (required)
ANIMESCHEDULE_APP_TOKEN=your_app_token_here

# AnimeSchedule OAuth2 app credentials — for user-level endpoints (required)
ANIMESCHEDULE_CLIENT_ID=your_oauth2_client_id_here
ANIMESCHEDULE_CLIENT_SECRET=your_oauth2_client_secret_here

# OAuth2 redirect URI — must exactly match what you registered on AnimeSchedule (required)
# Local:      http://localhost:8080/oauth/callback
# Production: https://yourdomain.com/oauth/callback
OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback

# Port for the built-in OAuth2 callback server (default: 8080)
OAUTH_CALLBACK_PORT=8080
```

## 🐳 Deployment

### Docker Compose (recommended)

```bash
docker compose up -d
```

The `docker-compose.yml` maps port `8080` for the OAuth2 callback server and passes all environment variables from your `.env` file.

### Plain Docker

```bash
docker build -t anime-discord-bot .

docker run -d \
  --name anime-discord-bot \
  --restart unless-stopped \
  -p 8080:8080 \
  -e DISCORD_TOKEN=... \
  -e ANIMESCHEDULE_APP_TOKEN=... \
  -e ANIMESCHEDULE_CLIENT_ID=... \
  -e ANIMESCHEDULE_CLIENT_SECRET=... \
  -e OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback \
  anime-discord-bot
```

### Local (no Docker)

```bash
pip install -r requirements.txt
# Set all env vars or populate a .env file, then:
python bot.py
```

### Production with a Domain

If hosting on a server with a public domain, set:

```env
OAUTH_REDIRECT_URI=https://yourdomain.com/oauth/callback
```

Register that same URI in your AnimeSchedule OAuth2 app settings. You can terminate TLS with nginx or Caddy in front:

```nginx
location /oauth/callback {
    proxy_pass http://127.0.0.1:8080/oauth/callback;
}
```

## 📦 Dependencies

| Package         | Purpose                                                    |
| --------------- | ---------------------------------------------------------- |
| `discord.py`    | Discord bot framework                                      |
| `aiohttp`       | Async HTTP client (API calls) + OAuth2 callback web server |
| `feedparser`    | RSS feed parsing                                           |
| `python-dotenv` | `.env` file loading                                        |

Install with:

```bash
pip install -r requirements.txt
```

## 🗂️ Project Structure

```text
anime-discord-bot/
├── bot.py              # All bot logic
├── feeds.txt           # RSS feed definitions (name|url per line)
├── bot_data.json       # Persistent storage (created automatically)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## ⚙️ Technical Notes

### Discord Limits Handled

- **6000 character embed limit** — `embed_char_count()` tracks exact character usage before each field is added. Embeds are split and sent as continuation messages automatically.
- **25 field limit per embed** — enforced alongside the character limit check.
- **10 embeds per message** — the `send_embeds()` helper batches into groups of 5 to stay well within the limit.
- **Rate limits** — visual mode sleeps 1.5 seconds every 5 embeds to avoid hitting Discord's per-channel rate limit.

### State is In-Memory

Feed subscriptions (`!feed enable`) and user OAuth2 tokens (`!login`) are stored in Python dictionaries for the lifetime of the process. If the bot restarts:

- Users need to run `!login` again to re-authorize
- Server admins need to run `!feed enable` again to re-subscribe channels

To persist state across restarts, the dictionaries can be serialized to a JSON file or SQLite database.

### API Field Names

The AnimeSchedule v3 API returns **camelCase** field names for timetable objects (e.g. `episodeDate`, `airType`, `imageVersionRoute`) and uses a nested `names` object in ShowDetail for `romaji`, `english`, and `japanese` titles. The bot reads both sources and merges them in the visual embed builder.

### Image URLs

Show images are served from `https://img.animeschedule.net/production/assets/public/img/` using the `imageVersionRoute` field (a versioned path separate from the show's URL `route`). The bot uses this field directly — do not substitute `route` for `imageVersionRoute` as they are different values.

### RSS feed handling

- Feeds are fetched live every 5 minutes.

- Only entries with a valid pubDate within the last 24 hours are posted.

- Images are extracted from common RSS fields (media:thumbnail, media:content, * enclosures, itunes:image, and <img> tags in description).

- Seen entry GUIDs are stored in bot_data.json to avoid reposts after restarts.

### State persistence

All subscriptions and seen entry IDs are saved to bot_data.json on every change, ensuring the bot remembers what it has posted even after a crash or restart.
