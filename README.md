# 🤖 Standup Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open Source](https://img.shields.io/badge/Open%20Source-%E2%9D%A4-brightgreen)](https://github.com/iamkingsleey/standup-agent)

A smart, open-source Slack bot that acts as your personal AI assistant — powered by Claude (Anthropic) and integrated with Google Calendar. It sends you a daily standup prompt, answers questions, manages your calendar, monitors channel mentions, and summarizes conversations, all from your Slack DMs.

Deployable to any cloud host. This project uses [Railway](https://railway.app) for production.

---

## Features

### 📅 Daily Standup
Every morning at 9:00 AM the bot DMs you with a "What are you working on today?" prompt alongside your Google Calendar events for the day. Works for every user who connects their own calendar.

### 🗓️ Google Calendar Integration (Per-User)
Each Slack user connects their own personal Google Calendar. The bot can:
- Show today's or tomorrow's events on demand
- Create new events from natural language ("schedule a design review tomorrow at 2pm")
- Delete events ("cancel the product sync today")
- Invite attendees by email when creating events

### 💬 Context-Aware AI Replies
DM the bot anything. It remembers the last 10 messages in your conversation so it can answer follow-up questions with full context. Powered by Claude.

### 📋 Channel & Thread Summarization
- **DM command:** "summarize #dev-team" or "what happened in #general" — the bot fetches the last 24 hours of messages and returns a structured summary with key topics, decisions, action items, and open questions.
- **Slash command:** `/summarize` in any channel or thread for an instant summary.
- **Auto-summary:** When a Slack thread hits 10+ replies, the bot automatically posts a summary inside the thread.

### 🕒 Timezone Intelligence
The bot detects time + timezone mentions in messages (e.g. "3 PM PST", "14:30 UTC") and converts them to your local timezone. It also flags times that fall outside standard business hours. Set your timezone with: "my timezone is WAT" or "set timezone to America/New_York".

### 👀 Mention Monitoring & Auto-Reply
If someone @mentions you in a channel and you don't respond within 5 minutes, the bot automatically replies on your behalf with a helpful AI-generated response. It skips auto-replies if you're showing as active on Slack or the message contains sensitive keywords.

### 🌐 Multi-Workspace Support
The bot supports multiple Slack workspaces simultaneously. Each workspace installs the bot via OAuth, and each individual user within a workspace can connect their own Google Calendar.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | [Slack Bolt for Python](https://slack.dev/bolt-python/) |
| Web server | Flask + Gunicorn |
| AI responses | [Anthropic Claude](https://www.anthropic.com/) (`claude-sonnet-4-20250514`) |
| Calendar | Google Calendar API v3 (OAuth 2.0) |
| Database | SQLite (persisted via Railway Volume) |
| Scheduler | `schedule` library (background thread) |
| Hosting | [Railway](https://railway.app) |

---

## Project Structure

```
standup-agent/
├── bot_scheduled.py     # Main application — all routes, handlers, and helpers
├── requirements.txt     # Python dependencies
├── railway.toml         # Railway deployment config (Gunicorn command)
├── Procfile             # Fallback process file
├── .env.example         # Template for required environment variables
└── .gitignore
```

---

## Setup Guide

### 1. Prerequisites

- A [Slack App](https://api.slack.com/apps) with the following scopes:
  `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `commands`, `groups:history`, `groups:read`, `im:history`, `im:write`, `users:read`
- An [Anthropic API key](https://console.anthropic.com/)
- A [Google Cloud project](https://console.cloud.google.com/) with the Calendar API enabled and an OAuth 2.0 **Web Application** client
- Python 3.10+

### 2. Clone and install

```bash
git clone https://github.com/iamkingsleey/standup-agent.git
cd standup-agent
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```env
SLACK_CLIENT_ID=your_slack_client_id
SLACK_CLIENT_SECRET=your_slack_client_secret
SLACK_SIGNING_SECRET=your_slack_signing_secret
SLACK_REDIRECT_URI=https://your-domain.com/slack/oauth_redirect

ANTHROPIC_API_KEY=your_anthropic_api_key

GOOGLE_CREDENTIALS_JSON={"web":{"client_id":"...","client_secret":"...",...}}
```

> **Note:** `GOOGLE_CREDENTIALS_JSON` should contain the full JSON content of your Google OAuth Web Application client credentials file. On Railway, paste it as a multi-line environment variable.

### 4. Configure your Slack App

In your [Slack App settings](https://api.slack.com/apps):

| Setting | Value |
|---|---|
| OAuth Redirect URL | `https://your-domain.com/slack/oauth_redirect` |
| Event Subscriptions URL | `https://your-domain.com/slack/events` |
| Events to subscribe to | `message.im`, `message.channels`, `message.groups`, `app_mention` |
| Slash Commands | `/summarize` → `https://your-domain.com/slack/events` |

### 5. Configure Google OAuth

In [Google Cloud Console](https://console.cloud.google.com/):

1. Enable the **Google Calendar API**
2. Create an OAuth 2.0 credential — type: **Web application**
3. Add an Authorized Redirect URI: `https://your-domain.com/auth/google/callback`
4. Download the JSON and paste its contents into the `GOOGLE_CREDENTIALS_JSON` env var

### 6. Run locally

```bash
python3 bot_scheduled.py
```

The server starts on port 3000 by default (override with `PORT` env var). Use [ngrok](https://ngrok.com/) to expose it for local Slack testing.

### 7. Deploy to Railway

1. Push this repo to GitHub
2. Create a new Railway project and connect the repo
3. Add a **Volume** mounted at `/app/data` (stores the SQLite database across deploys)
4. Set all environment variables in Railway's dashboard
5. Railway will auto-deploy on every push using the Gunicorn command in `railway.toml`

---

## Installation

Once deployed, install the bot in your Slack workspace by visiting:

```
https://your-domain.com/slack/install
```

After installing, DM the bot in Slack. It will automatically send you a personal Google Calendar link to connect your calendar.

---

## Usage

### Connecting Google Calendar
When you first DM the bot, it sends you a personal link:
```
https://your-domain.com/auth/google?team_id=T...&user_id=U...
```
Click it, approve Google's permissions, and your calendar is linked. Every user in the workspace gets their own unique link.

### DM commands

| What you say | What happens |
|---|---|
| "what's on my calendar today?" | Lists today's events |
| "what do I have tomorrow?" | Lists tomorrow's events |
| "schedule a team sync tomorrow at 3pm" | Creates a Google Calendar event |
| "cancel the product review today" | Deletes a matching event |
| "summarize #engineering" | Summarizes the last 24h of a channel |
| "my timezone is EST" | Sets your timezone for time conversions |
| Anything else | Conversational AI reply with calendar context |

### Slash command

```
/summarize             — summarize current thread or channel (last 24h)
/summarize #channel    — summarize a specific channel
```

---

## Database Schema

The bot uses a single SQLite file at `data/bot.db` with these tables:

| Table | Purpose |
|---|---|
| `installations` | Bot tokens per workspace (populated during OAuth install) |
| `oauth_states` | Short-lived CSRF state tokens for the Slack install flow |
| `workspace_owners` | Maps each workspace to its first DM user (gets daily standups) |
| `google_tokens` | Per-user Google Calendar OAuth tokens `(team_id, user_id)` |
| `user_timezones` | Per-user timezone preferences |

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `SLACK_CLIENT_ID` | ✅ | From Slack App → Basic Information |
| `SLACK_CLIENT_SECRET` | ✅ | From Slack App → Basic Information |
| `SLACK_SIGNING_SECRET` | ✅ | From Slack App → Basic Information |
| `SLACK_REDIRECT_URI` | ✅ | Full URL to `/slack/oauth_redirect` on your host |
| `ANTHROPIC_API_KEY` | ✅ | From [Anthropic Console](https://console.anthropic.com/) |
| `GOOGLE_CREDENTIALS_JSON` | ✅ | Full JSON content of your Google OAuth Web App client |
| `PORT` | ❌ | Server port (default: 3000; Railway sets this automatically) |

---

## Architecture Notes

- **HTTP mode only** — the bot uses Flask + Slack's Events API (webhook) rather than Socket Mode. This is required for cloud hosting and Slack App Directory distribution.
- **No Bolt OAuth** — the Slack OAuth install flow is handled manually via Flask routes to avoid conflicts with Railway's reverse proxy and URL rewriting.
- **Retry deduplication** — Slack retries events that don't get a < 3s response. The bot immediately returns 200 on retried requests (`X-Slack-Retry-Num` header) and deduplicates by `event_id` as a second layer.
- **Per-user calendar tokens** — Google OAuth tokens are stored with a `(team_id, user_id)` composite key so every Slack user in every workspace has their own independent calendar connection.

---

## Contributing

This project is open source and contributions are welcome! Whether it's a bug fix, new feature, or documentation improvement — feel free to get involved.

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-idea`)
3. Commit your changes (`git commit -m 'feat: add your idea'`)
4. Push to your fork (`git push origin feature/your-idea`)
5. Open a Pull Request

For major changes, please open an issue first to discuss what you'd like to change.

---

## License

[MIT](LICENSE)

---

*Built by [Kingsley Mkpandiok](https://github.com/iamkingsleey)*
