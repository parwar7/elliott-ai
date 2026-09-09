# Private Telegram Cloud Bot

This worker lets an allowlisted Telegram chat request Elliott analyses while the
local PC is off. Telegram is only the control and delivery surface. The existing
price-first Elliott pipeline remains responsible for analysis.

## Architecture

1. Telegram receives `/analyze SYMBOL` or `/blind SYMBOL`.
2. A Railway persistent service receives the command through Bot API long polling.
3. Twelve Data supplies same-provider OHLCV snapshots.
4. `ElliottAgent.analyze()` performs the initial technical count.
5. `ElliottAgent.resolve_degrees()` performs deterministic degree validation.
6. The technical result is stored before a concise message, Markdown report, and
   JSON record are returned to Telegram.

The worker does not place trades and cannot control desktop TradingView while the
PC is off.

## 1. Get the Private Chat ID

Open the new bot in Telegram and send `/start`. Then run this in PowerShell. Enter
the token only in the hidden prompt.

```powershell
$secureToken = Read-Host "Telegram bot token" -AsSecureString
$token = [System.Net.NetworkCredential]::new("", $secureToken).Password
$updates = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates"
$updates.result[-1].message.chat.id
Remove-Variable secureToken, token, updates
```

Keep the resulting integer as `TELEGRAM_ALLOWED_CHAT_IDS`. Never put a real token
or API key in `.env.example`, source code, a commit, or a chat message.

## 2. Test Locally

Open PowerShell in the repository:

```powershell
cd "C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in"
```

Load secrets into only that PowerShell process:

```powershell
$secureBot = Read-Host "Telegram bot token" -AsSecureString
$env:TELEGRAM_BOT_TOKEN = [System.Net.NetworkCredential]::new("", $secureBot).Password

$secureMarket = Read-Host "Twelve Data API key" -AsSecureString
$env:TWELVE_DATA_API_KEY = [System.Net.NetworkCredential]::new("", $secureMarket).Password

$secureOpenAI = Read-Host "OpenAI API key" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $secureOpenAI).Password

$env:TELEGRAM_ALLOWED_CHAT_IDS = "YOUR_NUMERIC_CHAT_ID"
$env:ELLIOTT_PROVIDER = "openai"
$env:ELLIOTT_MODEL = "YOUR_WORKING_OPENAI_MODEL"
$env:ELLIOTT_ENABLE_RSI = "true"
$env:TELEGRAM_MONITOR_INTERVAL_MINUTES = "0"

Remove-Variable secureBot, secureMarket, secureOpenAI
python -m elliott_ai.telegram_bot
```

Send `/help`, then `/blind MSFT` to the bot. The analysis uses six market-data
requests and two model calls, so completion can take several minutes. Stop the
local worker with `Ctrl+C` after testing.

## 3. Create the Railway Service

1. Sign in at Railway and create an empty project.
2. Add an empty persistent service named `elliott-telegram-bot`.
3. Add a Railway volume to that service and mount it at `/data`.
4. Open the service's **Variables** page.
5. Add the variables below and seal the four secrets.

```text
TELEGRAM_BOT_TOKEN=<secret>
TELEGRAM_ALLOWED_CHAT_IDS=<numeric chat ID, or comma-separated IDs>
TWELVE_DATA_API_KEY=<secret>
OPENAI_API_KEY=<secret>
ELLIOTT_PROVIDER=openai
ELLIOTT_MODEL=<the model that already works for this API project>
ELLIOTT_ENABLE_RSI=true
TELEGRAM_MONITOR_INTERVAL_MINUTES=0
TELEGRAM_MONITOR_BLIND=true
ELLIOTT_CLOUD_TIMEFRAMES=monthly,weekly,daily,4h,1h,15m
```

The Docker image already supplies `/app` as the workspace, `/data` as persistent
storage, and the current SQLite database as the first-run seed.

## 4. Deploy from Windows

Install the Railway CLI with Node.js 16 or newer:

```powershell
npm install -g @railway/cli
railway login
```

Link this folder to the empty Railway project and service, then deploy:

```powershell
cd "C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in"
railway link
railway up
```

Select the project, production environment, and `elliott-telegram-bot` service
when prompted. Railway detects the root `Dockerfile`. Check startup output with:

```powershell
railway logs
```

No public domain is required because the bot uses long polling.

## 5. Use the Bot

```text
/analyze MSFT
/analyze NASDAQ:GOOGL
/blind RKLB
/status
/watch MSFT
/unwatch MSFT
/watchlist
/help
```

`/analyze` permits reviewed project memory. `/blind` excludes previous
symbol-specific counts and performs a rules-only recount.

## 6. Enable Monitoring Carefully

Set `TELEGRAM_MONITOR_INTERVAL_MINUTES` to `60` or longer and redeploy. A value of
`0` disables monitoring. Every monitoring pass performs a complete multi-timeframe
analysis, which consumes Twelve Data credits and OpenAI tokens. The first pass
records a baseline. Later messages are sent only when the normalized active-wave
state, structural role, anchors, or invalidation state changes.

This is technical-state monitoring, not an automatic entry signal. A cheaper,
high-frequency level monitor should be implemented separately before attempting
15-minute checks.

## Security Rules

- Keep the bot private with `TELEGRAM_ALLOWED_CHAT_IDS`.
- Seal cloud secrets in Railway.
- Never accept arbitrary shell commands or user-supplied file paths from Telegram.
- Rotate a token immediately if it is exposed.
- Use one worker replica while SQLite and long polling are active.
- Back up the `/data` volume because it contains runs, reports, and the watchlist.
