# ⚽ SofaScore Football Match Intelligence Bot

Autonomous football intelligence and fixture tracking engine powered by **Playwright**, **FastAPI**, **SQLAlchemy**, and **Telegram Bot API**.

---

## 🌟 Key Features

- **🌐 Automated SofaScore Scraper:** Uses stealth Playwright browser automation to extract all scheduled games, live scores, tournament details, and match URLs from SofaScore without getting blocked.
- **⚡ Instant CLI Mode:** Run `python main.py --list-today` to immediately fetch and view today's football fixtures cleanly in your terminal.
- **🤖 Interactive Telegram Bot:**
  - `/today` or `/games` — View today's matches grouped by league.
  - `/live` — In-play live games with real-time minutes and scores.
  - `/top` — Filter by Top 5 European leagues & Champions League.
  - `/refresh` — Triggers an on-demand scrape.
  - Interactive inline keyboard buttons for league filtering.
- **📊 Modern Web Dashboard:** Real-time web UI with glassmorphism styling, live score updates via WebSockets, league tabs, team search, and match bookmarking.
- **💾 SQLite Persistence:** Stores match history, scores, and tracking metadata.

---

## 🚀 Getting Started

### 1. Installation

```bash
# Clone or enter repository
cd sportcrawl

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 2. Configuration (`.env`)

Configure your Telegram bot token and admin chat ID in `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_CHAT_ID=your_chat_id_here
SOFASCORE_POLL_INTERVAL_SECONDS=120
SOFASCORE_HEADLESS=true
FEATURED_LEAGUES=Premier League,UEFA Champions League,UEFA Europa League,LaLiga,Serie A,Bundesliga,Ligue 1,FA Cup,Brasileirão Betano,Major League Soccer
```

---

## 💻 Usage

### 1. Instant Terminal Match List (CLI)

```bash
# View all today's football fixtures
python main.py --list-today

# View only Top/Featured leagues today
python main.py --top
```

### 2. Full Bot Service (Telegram + Web UI + Live Monitoring)

```bash
python main.py
```

- **Web Dashboard:** Open [http://localhost:8000](http://localhost:8000)
- **Telegram Bot:** Send `/today` or `/live` to your bot.

---

## 📱 Telegram Commands

| Command | Description |
| :--- | :--- |
| `/today` or `/games` | List all today's football games grouped by league |
| `/live` | View in-play live matches with current score & minute |
| `/top` | View top European leagues & featured matches |
| `/refresh` | Force scrape latest match fixtures from SofaScore |
| `/help` | Show command reference & bot status |

---

## 📡 REST API Endpoints

- `GET /api/matches/today` — List today's scheduled matches
- `GET /api/matches/live` — List live in-progress matches
- `GET /api/matches/tournaments` — List tournaments playing today
- `POST /api/matches/{match_id}/bookmark` — Pin match to watchlist
- `POST /api/scrape/trigger` — Trigger fresh SofaScore scrape
- `GET /api/status` — Health check & stats
- `WS /ws` — Real-time live score WebSocket stream
