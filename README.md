# 🤖 Standup Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open Source](https://img.shields.io/badge/Open%20Source-%E2%9D%A4-brightgreen)](https://github.com/iamkingsleey/standup-agent)

A smart, open-source Slack bot that acts as your personal AI assistant — powered by Claude (Anthropic) and integrated with Google Calendar and Jira. It sends you a daily standup prompt, answers questions, manages your calendar, tracks your Jira tickets, monitors channel mentions, summarizes conversations, remembers your work context, and proactively keeps you on top of your day — all from your Slack DMs.

Deployable to any cloud host. This project uses [Railway](https://railway.app) for production.

---

## Features

### 📅 Daily Standup
Every morning at 9:00 AM the bot DMs you with a "What are you working on today?" prompt alongside your Google Calendar events for the day. It also includes any conflict warnings (back-to-back or overlapping meetings) and a list of tasks carried over from yesterday. Works for every user who connects their own calendar.

### 🗓️ Google Calendar Integration (Per-User)
Each Slack user connects their own personal Google Calendar. The bot can:
- Show today's or tomorrow's events on demand
- Create new events from natural language ("schedule a design review tomorrow at 2pm")
- Delete events ("cancel the product sync today")
- Invite attendees by email when creating events

### 🧠 Long-term Memory
The bot remembers things about you across sessions — current projects, teammates, deadlines, tools you use, and preferences. After every conversation, it quietly extracts key facts and stores them, then injects that context into every future reply so it always knows who it's talking to.

Try asking:
- *"What do you know about me?"*
- *"What was I working on last week?"*
- *"Show me my work history this month"*

### 📋 Action Item Tracker
When you reply to the morning standup, the bot automatically extracts your tasks for the day and tracks them. At 5 PM it checks back in: *"Here's what you said this morning — how'd it go?"*

Try asking:
- *"What are my tasks?"*
- *"What are my pending action items?"*
- Reply *"done"* at end of day to mark everything complete

### 🤖 Proactive Intelligence

**Pre-meeting briefings** — About 10 minutes before every calendar event, the bot DMs you a personalised prep pack: who's attending, the meeting agenda, and any relevant pending tasks. No setup required — it fires automatically.

**End-of-day follow-up** — At 5 PM, if you have pending action items from the day's standup, the bot checks in and asks how you got on.

**Weekly retrospective** — Every Friday at 5 PM, the bot auto-generates a personalised summary of your week based on your standup history, covering wins, recurring themes, blockers, and suggested focus for next week.

**Conflict detection** — The morning standup automatically flags back-to-back meetings or overlapping events on your calendar so you can fix them before the day starts.

### 🤖 Autonomous Scheduling
The bot can find free time and block your calendar without you having to open it.

Try asking:
- *"Find a time with sarah@company.com this week"* → bot checks your calendar and presents 3 available slots, then reply *"book option 2"* to confirm
- *"When am I free for a 30 minute call tomorrow?"*
- *"Block 2 hours of focus time"* → finds your next free window and creates a Focus Time calendar event
- *"Protect 3 hours for deep work this week"*

### 💬 Context-Aware AI Replies
DM the bot anything. It remembers the last 10 messages in your conversation for follow-up questions, and combines that with your long-term memory for truly personalised replies. Powered by Claude.

### 📋 Channel & Thread Summarization
- **DM command:** "summarize #dev-team" or "what happened in #general" — fetches the last 24 hours and returns a structured summary with key topics, decisions, action items, and open questions.
- **Slash command:** `/summarize` in any channel or thread for an instant summary.
- **Auto-summary:** When a Slack thread hits 10+ replies, the bot automatically posts a summary inside the thread.

### 🕒 Timezone Intelligence
The bot detects time + timezone mentions in messages (e.g. "3 PM PST", "14:30 UTC") and converts them to your local timezone. It also flags times outside standard business hours. Set your timezone with: *"my timezone is WAT"* or *"set timezone to America/New_York"*.

### 👀 Mention Monitoring & Auto-Reply
If someone @mentions you in a channel and you don't respond within 5 minutes, the bot automatically replies on your behalf with a helpful AI-generated response. It skips auto-replies if you're showing as active on Slack or the message contains sensitive keywords.

### 🎫 Jira Cloud Integration
The bot connects to your Jira Cloud instance so you can manage tickets without leaving Slack. It surfaces your open issues in the morning standup and lets you create, update, and track work via simple DM commands.

Try asking:
- *"Show my Jira tickets"* → lists all open issues assigned to you with priority emoji and clickable links
- *"Sprint progress"* → shows a visual progress bar of the active sprint (e.g. `█████░░░░░ 50% complete`)
- *"Create a bug: Login page crashes on mobile"* → opens a new Bug in your default project
- *"New story: Add dark mode"* → opens a Story in Jira
- *"Mark PROJ-42 as done"* → transitions the issue to Done (fuzzy status matching)
- *"My Jira email is me@company.com"* → registers your email so tickets are filtered to you

Setup requires three environment variables — see [Environment Variables Reference](#environment-variables-reference) below.

### 🌐 Multi-Workspace Support
The bot supports multiple Slack workspaces simultaneously. Each workspace installs the bot via OAuth, and each individual user within a workspace can connect their own Google Calendar and get their own personalised experience.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | [Slack Bolt for Python](https://slack.dev/bolt-python/) |
| Web server | Flask + Gunicorn |
| AI responses | [Anthropic Claude](https://www.anthropic.com/) (`claude-sonnet-4-20250514`) |
| Calendar | Google Calendar API v3 (OAuth 2.0) |
| Project tracking | Jira Cloud REST API v3 + Agile API |
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

# Optional — enables Jira integration
JIRA_BASE_URL=https://your-workspace.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=your_jira_api_token
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

### 6. Configure Jira (optional)

To enable Jira integration:

1. Log in to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and create an API token
2. Note your Jira Cloud base URL (e.g. `https://your-workspace.atlassian.net`)
3. Add three env vars — `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — to your Railway dashboard (or `.env` locally)
4. After deploying, DM the bot: *"my Jira email is you@company.com"* so it can filter issues to your account

The bot will automatically include your open Jira issues in the morning standup once configured.

### 7. Run locally

```bash
python3 bot_scheduled.py
```

The server starts on port 3000 by default (override with `PORT` env var). Use [ngrok](https://ngrok.com/) to expose it for local Slack testing.

### 8. Deploy to Railway

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

### Connecting Jira
Once you've added the three Jira env vars and redeployed, tell the bot your Atlassian email:
```
my Jira email is you@company.com
```
The bot stores this in your long-term memory and uses it to filter Jira issues to your account. Your open issues will then appear in every morning standup alongside your calendar.

### DM commands

| What you say | What happens |
|---|---|
| "what's on my calendar today?" | Lists today's events |
| "what do I have tomorrow?" | Lists tomorrow's events |
| "schedule a team sync tomorrow at 3pm" | Creates a Google Calendar event |
| "cancel the product review today" | Deletes a matching event |
| "find a time with X this week" | Finds 3 free slots and offers to book |
| "book option 2" | Books the chosen slot from find-a-time |
| "block 2 hours of focus time" | Auto-creates a Focus Time calendar event |
| "what are my tasks?" | Lists all pending action items |
| "done" | Marks today's action items as complete |
| "what was I working on last week?" | Shows standup history |
| "summarize #engineering" | Summarizes the last 24h of a channel |
| "my timezone is EST" | Sets your timezone for time conversions |
| "show my Jira tickets" | Lists your open Jira issues with priority and links |
| "sprint progress" | Shows a visual progress bar of the active sprint |
| "create a bug: Title here" | Opens a new Bug in Jira |
| "new story: Title here" | Opens a new Story in Jira |
| "mark PROJ-42 as done" | Transitions a Jira issue to a new status |
| "my Jira email is X@company.com" | Registers your Jira email for issue filtering |
| Anything else | Conversational AI reply with full memory context |

### Slash command

```
/summarize             — summarize current thread or channel (last 24h)
/summarize #channel    — summarize a specific channel
```

### Automatic proactive messages

| When | What the bot sends |
|---|---|
| 9:00 AM daily | Morning standup with calendar, conflict warnings, carryover tasks, and open Jira issues |
| ~10 min before each meeting | Pre-meeting briefing with attendees, agenda, and pending tasks |
| 5:00 PM daily | End-of-day check-in on today's action items |
| Every Friday 5:00 PM | Weekly retrospective based on your standup history |

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
| `standup_responses` | Daily standup replies per user for history and retros |
| `standup_sent` | Tracks which users received today's standup (for reply detection) |
| `action_items` | Tasks extracted from standups with pending/done/dismissed status |
| `user_memories` | Long-term key-value facts per user (projects, preferences, colleagues) |
| `briefings_sent` | Tracks which meeting briefings have been sent (prevents duplicates) |

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
| `JIRA_BASE_URL` | ❌ | Your Jira Cloud URL, e.g. `https://yourworkspace.atlassian.net` |
| `JIRA_EMAIL` | ❌ | The Atlassian account email used to generate the API token |
| `JIRA_API_TOKEN` | ❌ | API token from [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `PORT` | ❌ | Server port (default: 3000; Railway sets this automatically) |

---

## Architecture Notes

- **HTTP mode only** — the bot uses Flask + Slack's Events API (webhook) rather than Socket Mode. This is required for cloud hosting and Slack App Directory distribution.
- **No Bolt OAuth** — the Slack OAuth install flow is handled manually via Flask routes to avoid conflicts with Railway's reverse proxy and URL rewriting.
- **Retry deduplication** — Slack retries events that don't get a < 3s response. The bot immediately returns 200 on retried requests (`X-Slack-Retry-Num` header) and deduplicates by `event_id` as a second layer.
- **Per-user calendar tokens** — Google OAuth tokens are stored with a `(team_id, user_id)` composite key so every Slack user in every workspace has their own independent calendar connection.
- **Async background tasks** — memory extraction and action item parsing run in background daemon threads after each reply so they never slow down the user-facing response.
- **Proactive scheduler** — all proactive features (briefings, EOD follow-up, weekly retro) run in a single background scheduler thread using the `schedule` library, checked every 60 seconds.
- **Jira via service account** — Jira uses a single API token (Basic Auth) shared across the deployment. Per-user filtering is achieved by storing each user's Atlassian email in `user_memories` and passing it as a JQL `assignee` filter. No per-user OAuth is required.

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
