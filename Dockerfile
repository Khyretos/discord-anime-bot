FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY feeds.txt .

# Exposes the OAuth2 callback server port
EXPOSE 8080

# Environment variables — all must be set at runtime
# DISCORD_TOKEN               — Discord bot token
# ANIMESCHEDULE_APP_TOKEN     — App-level API token (for timetables, shows, etc.)
# ANIMESCHEDULE_CLIENT_ID     — OAuth2 client ID
# ANIMESCHEDULE_CLIENT_SECRET — OAuth2 client secret
# OAUTH_REDIRECT_URI          — The public URL for /oauth/callback (must match what you registered)
# OAUTH_CALLBACK_PORT         — Port for the built-in callback server (default: 8080)

CMD ["python", "-u", "bot.py"]