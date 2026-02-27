"""
Standup Agent - Slack Bot with Google Calendar Integration
==========================================================
A Slack bot that acts as a personal AI assistant. It:
  - Sends a daily standup prompt every morning at 9:00 AM with your calendar for the day
  - Responds to direct messages with AI-powered answers and calendar context
  - Monitors channel mentions and auto-responds on your behalf if you haven't replied within 5 minutes
  - Responds directly when @mentioned in any channel it has been added to
  - Supports creating, reading, and deleting Google Calendar events via natural language in DMs

Transport:
  This bot uses HTTP mode (Flask) — Slack sends events to a public webhook URL.
  This is required for Slack App Directory distribution and cloud hosting on Railway.

Multi-Workspace:
  The bot supports multiple Slack workspaces via OAuth. Any workspace can install it
  by visiting /slack/install. Each workspace's bot token and owner are stored in a
  local SQLite database (data/bot.db). The workspace owner is the first person to DM
  the bot after installation — they get the standup messages and mention monitoring.

Setup:
  1. Copy .env.example to .env and fill in your credentials (including SLACK_CLIENT_ID
     and SLACK_CLIENT_SECRET from your Slack app's Basic Information page)
  2. Place your Google OAuth credentials in credentials.json
  3. Run: python3 bot_scheduled.py
  4. Visit https://<your-railway-url>/slack/install to install the bot in a workspace
  5. Set the Slack OAuth Redirect URL to: https://<your-railway-url>/slack/oauth_redirect

Dependencies:
  slack-bolt, flask, anthropic, google-auth, google-api-python-client, schedule, python-dotenv

Author: Kingsley Mkpandiok
"""

import os
import re
import sqlite3
import secrets
import datetime
import pickle
import schedule
import time
import threading
import json
import pytz

import requests as http_requests
from flask import Flask, request, jsonify, redirect as flask_redirect
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient
from anthropic import Anthropic
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# App Initialization
# ---------------------------------------------------------------------------

def authorize(enterprise_id, team_id, logger):
    """
    Bolt authorize callback — looks up the bot token for the requesting workspace.

    Slack Bolt calls this on every incoming event to get the correct bot token
    for the workspace that sent the event. We look it up from our SQLite database
    where it was stored during the OAuth installation flow.

    Args:
        enterprise_id: Enterprise Grid ID (None for standard workspaces).
        team_id (str): The Slack workspace/team ID.
        logger: Bolt's built-in logger.

    Returns:
        dict: A dict containing 'bot_token' for the workspace.

    Raises:
        Exception: If no installation is found for the workspace.
    """
    token = get_installation_token(team_id)
    if token:
        return AuthorizeResult(
            enterprise_id=enterprise_id,
            team_id=team_id,
            bot_token=token,
        )
    raise Exception(f"No installation found for team {team_id}")


# Slack Bolt app using a manual authorize callback instead of OAuthSettings.
# This bypasses Bolt's built-in OAuth machinery entirely, giving us full
# control over the installation flow and token storage.
app = App(
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    authorize=authorize,
)

# Flask web server — Slack sends all events to this server via HTTP POST
flask_app = Flask(__name__)

# SlackRequestHandler bridges Flask and Slack Bolt
handler = SlackRequestHandler(app)

# Anthropic client — used for all AI-generated responses
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ---------------------------------------------------------------------------
# Constants & Global State
# ---------------------------------------------------------------------------

# Google Calendar OAuth scopes — full access needed to create and delete events
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Dictionary tracking channel mentions that haven't received a reply yet.
# Key format: "{team_id}:{channel_id}:{thread_ts}"
# Value: dict with team_id, channel, thread_ts, original message text, and timestamp
pending_mentions: dict = {}

# Cache of recently processed Slack event IDs to prevent duplicate processing.
# Slack retries events if it doesn't get a response within 3 seconds — this
# ensures we never process the same event twice even if Slack resends it.
processed_event_ids: set = set()

# Messages containing any of these keywords will be skipped by the auto-responder
# to avoid the bot inadvertently weighing in on sensitive conversations.
SENSITIVE_KEYWORDS = ['personal', 'private', 'confidential', 'sensitive', '1:1', 'one-on-one']

# Per-user DM conversation history for multi-turn context-aware replies.
# Key: "{team_id}:{user_id}", Value: list of {"role": ..., "content": ...}
# Capped at 10 messages per user to stay within token limits.
conversation_history: dict = {}

# Tracks reply counts per thread for auto-summarization.
# Key: "{team_id}:{channel}:{thread_ts}", Value: int reply count
thread_reply_counts: dict = {}

# ---------------------------------------------------------------------------
# Jira Configuration (loaded once at startup from environment variables)
# ---------------------------------------------------------------------------

JIRA_BASE_URL  = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL     = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")


def jira_available() -> bool:
    """Return True if all three Jira env vars are set."""
    return bool(JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN)


def jira_headers() -> dict:
    """Build the Basic-Auth + JSON headers required by Jira Cloud's REST API."""
    import base64
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Jira Integration — Core Functions
# ---------------------------------------------------------------------------

def get_my_jira_issues(assignee_email: str | None = None) -> str:
    """
    Fetch open Jira issues assigned to a user and return a formatted Slack string.

    If assignee_email is provided, filters by that address. Otherwise falls back
    to the service-account user (currentUser()) set in JIRA_EMAIL.
    """
    if not jira_available():
        return (
            "⚠️ Jira isn't connected yet. Ask your admin to add "
            "`JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` to Railway."
        )

    # Determine assignee: use provided email, fall back to the service-account email
    effective_email = assignee_email or JIRA_EMAIL
    jql = (
        f'assignee = "{effective_email}" AND resolution = Unresolved '
        f'ORDER BY priority DESC, updated DESC'
    )

    try:
        # Use Atlassian's current POST-based JQL search endpoint
        resp = http_requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=jira_headers(),
            json={"jql": jql, "maxResults": 10,
                  "fields": ["summary", "status", "priority", "issuetype", "project"]},
            timeout=10,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])

        if not issues:
            return "✅ No open Jira issues assigned to you right now — you're clear!"

        priority_emoji = {
            "Highest": "🔴", "High": "🟠", "Medium": "🟡",
            "Low": "🟢", "Lowest": "⚪",
        }
        lines = [f"🎯 *Your open Jira issues ({len(issues)}):*\n"]
        for issue in issues:
            key     = issue["key"]
            summary = issue["fields"]["summary"]
            status  = issue["fields"]["status"]["name"]
            pri     = issue["fields"].get("priority", {}).get("name", "")
            emoji   = priority_emoji.get(pri, "⚪")
            url     = f"{JIRA_BASE_URL}/browse/{key}"
            lines.append(f"{emoji} *<{url}|{key}>* — {summary}\n   _{status}_")

        return "\n\n".join(lines)

    except Exception as e:
        return f"Jira error fetching issues: {e}"


def create_jira_issue(summary: str, description: str = "",
                      issue_type: str = "Task", project_key: str = "") -> str:
    """
    Create a new Jira issue, auto-detecting the project if none is specified.
    Returns a confirmation message with the issue key and link.
    """
    if not jira_available():
        return "⚠️ Jira isn't connected. Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN in Railway."

    if not project_key:
        try:
            r = http_requests.get(
                f"{JIRA_BASE_URL}/rest/api/3/project",
                headers=jira_headers(), timeout=10
            )
            r.raise_for_status()
            projects = r.json()
            if not projects:
                return "⚠️ No Jira projects found. Check the API token permissions."
            project_key = projects[0]["key"]
        except Exception as e:
            return f"Jira error finding project: {e}"

    body = {
        "fields": {
            "project":   {"key": project_key},
            "summary":   summary,
            "issuetype": {"name": issue_type},
            "description": {
                "type": "doc", "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description or summary}]
                }]
            },
        }
    }

    try:
        resp = http_requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue",
            headers=jira_headers(), json=body, timeout=10
        )
        resp.raise_for_status()
        key = resp.json()["key"]
        url = f"{JIRA_BASE_URL}/browse/{key}"
        return f"✅ Created *<{url}|{key}>*: {summary}\n🔗 {url}"
    except Exception as e:
        try:
            errors = resp.json().get("errorMessages") or resp.json().get("errors")
            return f"Jira error: {errors}"
        except Exception:
            return f"Jira error creating issue: {e}"


def update_jira_issue_status(issue_key: str, target_status: str) -> str:
    """
    Transition a Jira issue to the closest matching status.

    Fetches the available transitions first and does a case-insensitive
    substring match so users can say "done", "in progress", "to do", etc.
    """
    if not jira_available():
        return "⚠️ Jira isn't connected."

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key.upper()}/transitions"
    try:
        resp = http_requests.get(url, headers=jira_headers(), timeout=10)
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])

        match = next(
            (t for t in transitions if target_status.lower() in t["name"].lower()), None
        )
        if not match:
            available = ", ".join(t["name"] for t in transitions)
            return (
                f"⚠️ Can't move *{issue_key}* to '{target_status}'.\n"
                f"Available transitions: {available}"
            )

        http_requests.post(
            url, headers=jira_headers(),
            json={"transition": {"id": match["id"]}}, timeout=10
        ).raise_for_status()

        issue_url = f"{JIRA_BASE_URL}/browse/{issue_key.upper()}"
        return f"✅ *<{issue_url}|{issue_key.upper()}>* moved to *{match['name']}*"

    except Exception as e:
        return f"Jira error updating status: {e}"


def get_sprint_progress() -> str:
    """
    Return a visual progress bar and counts for the current active sprint.

    Looks up the first Jira Software board, finds its active sprint, and
    groups issues by status category (To Do / In Progress / Done).
    """
    if not jira_available():
        return "⚠️ Jira isn't connected."

    try:
        r = http_requests.get(
            f"{JIRA_BASE_URL}/rest/agile/1.0/board",
            headers=jira_headers(), timeout=10
        )
        r.raise_for_status()
        boards = r.json().get("values", [])
        if not boards:
            return "No Jira boards found."
        board_id, board_name = boards[0]["id"], boards[0]["name"]

        r = http_requests.get(
            f"{JIRA_BASE_URL}/rest/agile/1.0/board/{board_id}/sprint",
            headers=jira_headers(), params={"state": "active"}, timeout=10
        )
        r.raise_for_status()
        sprints = r.json().get("values", [])
        if not sprints:
            return f"No active sprint on *{board_name}* right now."

        sprint      = sprints[0]
        sprint_id   = sprint["id"]
        sprint_name = sprint["name"]
        end_date    = (sprint.get("endDate") or "")[:10] or "—"

        r = http_requests.get(
            f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}/issue",
            headers=jira_headers(),
            params={"maxResults": 200, "fields": "status,assignee,summary"},
            timeout=15
        )
        r.raise_for_status()
        issues = r.json().get("issues", [])

        done        = sum(1 for i in issues if i["fields"]["status"]["statusCategory"]["key"] == "done")
        in_progress = sum(1 for i in issues if i["fields"]["status"]["statusCategory"]["key"] == "indeterminate")
        todo        = sum(1 for i in issues if i["fields"]["status"]["statusCategory"]["key"] == "new")
        total       = len(issues)
        pct         = int(done / total * 100) if total else 0
        bar         = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))

        return (
            f"📊 *{sprint_name}* — {board_name}\n"
            f"🗓️ Ends: {end_date}\n\n"
            f"`{bar}` *{pct}% complete*\n\n"
            f"✅ Done: *{done}*   "
            f"🔄 In Progress: *{in_progress}*   "
            f"📋 To Do: *{todo}*   "
            f"📌 Total: *{total}*"
        )

    except Exception as e:
        return f"Jira error fetching sprint: {e}"


# ---------------------------------------------------------------------------
# Database — Per-Workspace Owner Storage
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Initialize the SQLite database and create all required tables.

    Tables created:
      - installations: stores bot tokens per workspace, populated during OAuth.
      - oauth_states: short-lived CSRF state tokens used during the OAuth handshake.
      - workspace_owners: maps each workspace to the user who first DM'd the bot.

    Should be called once at startup before the web server starts.
    """
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/bot.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS installations (
            team_id      TEXT PRIMARY KEY,
            team_name    TEXT,
            bot_token    TEXT NOT NULL,
            bot_user_id  TEXT,
            installed_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state      TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace_owners (
            team_id      TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            installed_at TEXT NOT NULL
        )
    """)
    # --- google_tokens: migrate from per-workspace to per-user if needed ---
    cursor = conn.execute("PRAGMA table_info(google_tokens)")
    existing_columns = [row[1] for row in cursor.fetchall()]

    if existing_columns and 'user_id' not in existing_columns:
        # Old schema (team_id PRIMARY KEY) — rename and recreate
        conn.execute("ALTER TABLE google_tokens RENAME TO google_tokens_v1")
        conn.execute("""
            CREATE TABLE google_tokens (
                team_id    TEXT NOT NULL,
                user_id    TEXT NOT NULL DEFAULT '',
                token_data BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (team_id, user_id)
            )
        """)
        # Migrate old rows — assign them to the workspace owner so the existing
        # calendar connection is preserved for the person who originally set it up
        conn.execute("""
            INSERT INTO google_tokens (team_id, user_id, token_data, updated_at)
            SELECT g.team_id, COALESCE(w.user_id, ''), g.token_data, g.updated_at
            FROM google_tokens_v1 g
            LEFT JOIN workspace_owners w ON g.team_id = w.team_id
        """)
        conn.execute("DROP TABLE google_tokens_v1")
        print("Migrated google_tokens table to per-user schema.")
    elif not existing_columns:
        # Fresh install — create with the new schema from the start
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_tokens (
                team_id    TEXT NOT NULL,
                user_id    TEXT NOT NULL DEFAULT '',
                token_data BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (team_id, user_id)
            )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_timezones (
            team_id    TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            timezone   TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (team_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS standup_responses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id    TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            response   TEXT NOT NULL,
            date       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS action_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id    TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            task       TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            team_id      TEXT NOT NULL,
            user_id      TEXT NOT NULL,
            memory_key   TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (team_id, user_id, memory_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS briefings_sent (
            team_id  TEXT NOT NULL,
            user_id  TEXT NOT NULL,
            event_id TEXT NOT NULL,
            sent_at  TEXT NOT NULL,
            PRIMARY KEY (team_id, user_id, event_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS standup_sent (
            team_id   TEXT NOT NULL,
            user_id   TEXT NOT NULL,
            sent_date TEXT NOT NULL,
            PRIMARY KEY (team_id, user_id, sent_date)
        )
    """)
    conn.commit()
    conn.close()


def store_oauth_state(state: str) -> None:
    """
    Persist a short-lived OAuth state token to the database.

    The state token is generated during /slack/install and verified when
    Slack redirects back to /slack/oauth_redirect. It prevents CSRF attacks
    by ensuring the redirect came from our own install page.

    Args:
        state (str): A cryptographically random URL-safe string.
    """
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR REPLACE INTO oauth_states (state, created_at) VALUES (?, ?)",
        (state, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def verify_and_consume_state(state: str) -> bool:
    """
    Verify a state token exists and delete it so it cannot be reused.

    Args:
        state (str): The state value received from Slack's redirect.

    Returns:
        bool: True if the state was found and deleted, False if not found.
    """
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT state FROM oauth_states WHERE state = ?", (state,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        conn.commit()
    conn.close()
    return row is not None


def store_installation(team_id: str, team_name: str, bot_token: str, bot_user_id: str) -> None:
    """
    Save a workspace's bot token after a successful OAuth installation.

    Args:
        team_id (str): Slack workspace/team ID.
        team_name (str): Human-readable workspace name.
        bot_token (str): The bot's OAuth access token (starts with xoxb-).
        bot_user_id (str): The Slack user ID of the bot itself in this workspace.
    """
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        """INSERT OR REPLACE INTO installations
           (team_id, team_name, bot_token, bot_user_id, installed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (team_id, team_name, bot_token, bot_user_id, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_installation_token(team_id: str) -> str | None:
    """
    Retrieve the stored bot token for a given workspace.

    Args:
        team_id (str): Slack workspace/team ID.

    Returns:
        str | None: The bot token, or None if not installed.
    """
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT bot_token FROM installations WHERE team_id = ?", (team_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_workspace_owner(team_id: str) -> str | None:
    """
    Look up the owner user ID for a given Slack workspace.

    Args:
        team_id (str): The Slack team/workspace ID (e.g. 'T01234567').

    Returns:
        str | None: The Slack user ID of the workspace owner, or None if no
                    owner has been recorded for this workspace yet.
    """
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT user_id FROM workspace_owners WHERE team_id = ?", (team_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def set_workspace_owner(team_id: str, user_id: str) -> None:
    """
    Record the owner user ID for a given Slack workspace.

    Uses INSERT OR REPLACE so calling this again with a new user ID will
    update the owner. This lets the workspace owner be reassigned if needed.

    Args:
        team_id (str): The Slack team/workspace ID.
        user_id (str): The Slack user ID to designate as workspace owner.
    """
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR REPLACE INTO workspace_owners (team_id, user_id, installed_at) VALUES (?, ?, ?)",
        (team_id, user_id, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_all_workspaces() -> list[tuple]:
    """
    Retrieve all registered workspace owners from the database.

    Used by the daily standup scheduler to send morning messages to every
    workspace that has an owner set up.

    Returns:
        list[tuple]: A list of (team_id, user_id) pairs for all workspaces.
    """
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute("SELECT team_id, user_id FROM workspace_owners").fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Standup Response Storage
# ---------------------------------------------------------------------------

def save_standup_response(team_id: str, user_id: str, response: str) -> None:
    """Persist a user's standup reply for history and weekly retro generation."""
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT INTO standup_responses (team_id, user_id, response, date, created_at) VALUES (?, ?, ?, ?, ?)",
        (team_id, user_id, response, datetime.date.today().isoformat(),
         datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_standup_history(team_id: str, user_id: str, days: int = 30) -> list:
    """Retrieve the user's standup responses for the past N days."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute(
        "SELECT date, response FROM standup_responses "
        "WHERE team_id=? AND user_id=? AND date>=? ORDER BY date DESC",
        (team_id, user_id, since)
    ).fetchall()
    conn.close()
    return [{"date": r[0], "response": r[1]} for r in rows]


def mark_standup_sent(team_id: str, user_id: str) -> None:
    """Record that today's standup was sent to this user."""
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR IGNORE INTO standup_sent (team_id, user_id, sent_date) VALUES (?, ?, ?)",
        (team_id, user_id, datetime.date.today().isoformat())
    )
    conn.commit()
    conn.close()


def standup_sent_today(team_id: str, user_id: str) -> bool:
    """Check whether we already sent today's standup to this user."""
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT 1 FROM standup_sent WHERE team_id=? AND user_id=? AND sent_date=?",
        (team_id, user_id, datetime.date.today().isoformat())
    ).fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------------
# Action Item Storage
# ---------------------------------------------------------------------------

def save_action_items(team_id: str, user_id: str, items: list) -> int:
    """Persist a list of extracted tasks. Returns the number saved."""
    if not items:
        return 0
    conn = sqlite3.connect("data/bot.db")
    now = datetime.datetime.utcnow().isoformat()
    count = 0
    for item in items:
        if item and item.strip():
            conn.execute(
                "INSERT INTO action_items (team_id, user_id, task, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (team_id, user_id, item.strip(), now, now)
            )
            count += 1
    conn.commit()
    conn.close()
    return count


def get_pending_action_items(team_id: str, user_id: str) -> list:
    """Return all pending action items for a user."""
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute(
        "SELECT id, task, created_at FROM action_items "
        "WHERE team_id=? AND user_id=? AND status='pending' ORDER BY created_at DESC",
        (team_id, user_id)
    ).fetchall()
    conn.close()
    return [{"id": r[0], "task": r[1], "created_at": r[2]} for r in rows]


def get_todays_action_items(team_id: str, user_id: str) -> list:
    """Return pending action items created today."""
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute(
        "SELECT id, task FROM action_items "
        "WHERE team_id=? AND user_id=? AND status='pending' AND date(created_at)=?",
        (team_id, user_id, today)
    ).fetchall()
    conn.close()
    return [{"id": r[0], "task": r[1]} for r in rows]


def mark_all_todays_items_done(team_id: str, user_id: str) -> int:
    """Mark all of today's pending action items as done. Returns count updated."""
    today = datetime.date.today().isoformat()
    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect("data/bot.db")
    cursor = conn.execute(
        "UPDATE action_items SET status='done', updated_at=? "
        "WHERE team_id=? AND user_id=? AND status='pending' AND date(created_at)=?",
        (now, team_id, user_id, today)
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def dismiss_all_pending_items(team_id: str, user_id: str) -> None:
    """Mark all pending action items as dismissed (user said they're not relevant)."""
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "UPDATE action_items SET status='dismissed', updated_at=? WHERE team_id=? AND user_id=? AND status='pending'",
        (datetime.datetime.utcnow().isoformat(), team_id, user_id)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Long-term Memory
# ---------------------------------------------------------------------------

def get_user_memories(team_id: str, user_id: str) -> dict:
    """Return all stored memory key-value pairs for a user."""
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute(
        "SELECT memory_key, memory_value FROM user_memories WHERE team_id=? AND user_id=?",
        (team_id, user_id)
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def update_user_memory(team_id: str, user_id: str, key: str, value: str) -> None:
    """Upsert a single memory fact for a user."""
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR REPLACE INTO user_memories (team_id, user_id, memory_key, memory_value, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (team_id, user_id, key, value, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def build_memory_context(team_id: str, user_id: str) -> str:
    """Format stored memories as a string to inject into the AI system prompt."""
    memories = get_user_memories(team_id, user_id)
    if not memories:
        return ""
    lines = ["Long-term memory about this user:"]
    for key, value in memories.items():
        lines.append(f"  • {key}: {value}")
    return "\n".join(lines)


def extract_and_update_memories_async(team_id: str, user_id: str, conversation: str) -> None:
    """
    Run in a background thread: ask Claude to extract memorable facts from a
    conversation and persist them to user_memories. Keeps the DM response fast
    since this runs after the reply has already been sent.
    """
    def _run():
        try:
            existing = get_user_memories(team_id, user_id)
            existing_str = json.dumps(existing) if existing else "{}"
            response = anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract key long-term facts worth remembering about the user from this conversation.\n"
                        "Focus on: active projects, deadlines, teammates, tools/tech, preferences, ongoing blockers.\n"
                        "Return ONLY a JSON object where keys are short category labels and values are brief descriptions.\n"
                        "Only include genuinely new or meaningfully updated facts. Return {} if nothing notable.\n"
                        f"Existing memory: {existing_str}\n\n"
                        f"Conversation:\n{conversation}"
                    )
                }]
            )
            text = response.content[0].text.strip().replace('```json', '').replace('```', '').strip()
            new_facts = json.loads(text)
            for key, value in new_facts.items():
                if key and value:
                    update_user_memory(team_id, user_id, key, str(value))
        except Exception as e:
            print(f"Memory extraction error: {e}")

    threading.Thread(target=_run, daemon=True).start()


def extract_action_items_async(team_id: str, user_id: str, text: str) -> None:
    """
    Run in a background thread: ask Claude to pull out concrete tasks from a
    message and persist them to action_items.
    """
    def _run():
        try:
            response = anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract specific action items / tasks from this standup or message.\n"
                        "Return ONLY a JSON array of short task strings (each under 100 chars).\n"
                        "Only include concrete things the person said they will do today. Return [] if none.\n\n"
                        f"Text: {text}"
                    )
                }]
            )
            raw = response.content[0].text.strip().replace('```json', '').replace('```', '').strip()
            items = json.loads(raw)
            saved = save_action_items(team_id, user_id, [str(i) for i in items if i])
            if saved:
                print(f"Saved {saved} action items for user {user_id}")
        except Exception as e:
            print(f"Action item extraction error: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Pre-meeting Briefings
# ---------------------------------------------------------------------------

def get_all_calendar_users() -> list:
    """Return all (team_id, user_id) pairs that have Google Calendar connected."""
    conn = sqlite3.connect("data/bot.db")
    rows = conn.execute(
        "SELECT team_id, user_id FROM google_tokens WHERE user_id != ''"
    ).fetchall()
    conn.close()
    return list(rows)


def has_briefing_been_sent(team_id: str, user_id: str, event_id: str) -> bool:
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT 1 FROM briefings_sent WHERE team_id=? AND user_id=? AND event_id=?",
        (team_id, user_id, event_id)
    ).fetchone()
    conn.close()
    return row is not None


def record_briefing_sent(team_id: str, user_id: str, event_id: str) -> None:
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR IGNORE INTO briefings_sent (team_id, user_id, event_id, sent_at) VALUES (?, ?, ?, ?)",
        (team_id, user_id, event_id, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def generate_meeting_briefing(event: dict, team_id: str, user_id: str) -> str:
    """
    Build a short pre-meeting prep DM for an upcoming calendar event.

    Pulls the event title, attendees, description, and the user's pending
    action items, then asks Claude to format a concise briefing.
    """
    title = event.get('summary', 'Untitled Meeting')
    start_raw = event.get('start', {}).get('dateTime', '')
    try:
        start_dt = datetime.datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
        start_str = start_dt.strftime('%I:%M %p')
    except Exception:
        start_str = start_raw

    attendees = [
        a.get('email', '') for a in event.get('attendees', [])
        if not a.get('self') and a.get('email')
    ]
    description = (event.get('description') or '')[:300]
    pending = get_pending_action_items(team_id, user_id)
    items_str = "\n".join(f"• {i['task']}" for i in pending[:5]) if pending else "None"

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=350,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a concise pre-meeting briefing for a Slack DM (under 180 words).\n"
                    f"Meeting: {title} at {start_str}\n"
                    f"Attendees: {', '.join(attendees) if attendees else 'Team only'}\n"
                    f"Agenda: {description or 'Not provided'}\n"
                    f"User's pending tasks: {items_str}\n\n"
                    f"Include: who's attending, key agenda points, any relevant pending tasks, "
                    f"and 1-2 quick prep tips. Be direct and practical."
                )
            }]
        )
        body = response.content[0].text
    except Exception:
        body = f"You have *{title}* with {', '.join(attendees) if attendees else 'your team'}. Make sure you're prepared!"

    return f"📋 *Meeting in ~10 minutes: {title}*\n\n{body}"


def check_and_send_meeting_briefings() -> None:
    """
    Scheduled every 5 minutes. Finds calendar events starting in 8-12 minutes
    for all connected users and sends a pre-meeting briefing DM if not yet sent.
    """
    users = get_all_calendar_users()
    now_utc = datetime.datetime.utcnow()

    for team_id, user_id in users:
        try:
            bot_token = get_installation_token(team_id)
            if not bot_token:
                continue

            service = get_calendar_service(team_id, user_id)
            if not service:
                continue

            window_start = (now_utc + datetime.timedelta(minutes=8)).isoformat() + 'Z'
            window_end   = (now_utc + datetime.timedelta(minutes=12)).isoformat() + 'Z'

            result = service.events().list(
                calendarId='primary',
                timeMin=window_start,
                timeMax=window_end,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            for event in result.get('items', []):
                event_id = event.get('id')
                if not event_id or has_briefing_been_sent(team_id, user_id, event_id):
                    continue
                briefing = generate_meeting_briefing(event, team_id, user_id)
                WebClient(token=bot_token).chat_postMessage(channel=user_id, text=briefing)
                record_briefing_sent(team_id, user_id, event_id)
                print(f"Briefing sent to {user_id} for '{event.get('summary')}'")

        except Exception as e:
            print(f"Briefing check error for {user_id} in {team_id}: {e}")


# ---------------------------------------------------------------------------
# End-of-day Follow-up
# ---------------------------------------------------------------------------

def send_eod_followup() -> None:
    """
    Scheduled at 17:00. If a user has pending action items from today's standup,
    send a friendly check-in asking how they got on.
    """
    users = get_all_calendar_users()
    for team_id, user_id in users:
        try:
            items = get_todays_action_items(team_id, user_id)
            if not items:
                continue
            bot_token = get_installation_token(team_id)
            if not bot_token:
                continue
            task_list = "\n".join(
                f"{i + 1}. {item['task']}" for i, item in enumerate(items)
            )
            message = (
                f"👋 *End-of-day check-in!*\n\n"
                f"Here are the tasks you mentioned this morning:\n{task_list}\n\n"
                f"How'd it go? Reply *done* to mark them all complete, or tell me what's still in progress."
            )
            WebClient(token=bot_token).chat_postMessage(channel=user_id, text=message)
            print(f"EOD follow-up sent to {user_id} in {team_id}")
        except Exception as e:
            print(f"EOD follow-up error for {user_id} in {team_id}: {e}")


# ---------------------------------------------------------------------------
# Weekly Retrospective
# ---------------------------------------------------------------------------

def send_weekly_retro() -> None:
    """
    Scheduled every Friday at 17:00. Generates a personalised weekly retro
    from the user's standup history and posts it as a DM.
    """
    workspaces = get_all_workspaces()
    for team_id, user_id in workspaces:
        try:
            history = get_standup_history(team_id, user_id, days=7)
            if not history:
                continue
            bot_token = get_installation_token(team_id)
            if not bot_token:
                continue

            history_text = "\n\n".join(
                f"*{e['date']}:* {e['response']}" for e in reversed(history)
            )
            response = anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=700,
                messages=[{
                    "role": "user",
                    "content": (
                        "Generate a friendly, personal weekly retrospective from this person's standup updates.\n"
                        "Structure it exactly as:\n"
                        "🏆 *Wins this week*\n"
                        "🔄 *Recurring themes*\n"
                        "🚧 *Blockers & challenges*\n"
                        "🎯 *Suggested focus for next week*\n\n"
                        "Keep it encouraging, specific, and under 250 words.\n\n"
                        f"Standup history:\n{history_text}"
                    )
                }]
            )
            retro = response.content[0].text
            week_str = datetime.date.today().strftime('%B %d')
            WebClient(token=bot_token).chat_postMessage(
                channel=user_id,
                text=f"🗓️ *Weekly Retro — week of {week_str}*\n\n{retro}"
            )
            print(f"Weekly retro sent to {user_id} in {team_id}")
        except Exception as e:
            print(f"Weekly retro error for {user_id} in {team_id}: {e}")


# ---------------------------------------------------------------------------
# Autonomous Scheduling — Find Free Slots & Focus Time
# ---------------------------------------------------------------------------

def find_free_slots(team_id: str, user_id: str, duration_minutes: int = 60,
                    days_ahead: int = 5) -> list:
    """
    Scan the user's calendar over the next N business days and return up to 3
    free windows long enough to accommodate `duration_minutes`.

    Returns a list of dicts: {start, end, date_str, time_str}
    """
    service = get_calendar_service(team_id, user_id)
    if not service:
        return []

    slots = []
    now = datetime.datetime.utcnow()

    for day_offset in range(1, days_ahead + 1):
        target = now + datetime.timedelta(days=day_offset)
        if target.weekday() >= 5:          # skip weekends
            continue

        day_start = target.replace(hour=9,  minute=0, second=0, microsecond=0)
        day_end   = target.replace(hour=18, minute=0, second=0, microsecond=0)

        result = service.events().list(
            calendarId='primary',
            timeMin=day_start.isoformat() + 'Z',
            timeMax=day_end.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        # Build sorted list of busy (start, end) pairs
        busy = []
        for ev in result.get('items', []):
            s = ev['start'].get('dateTime')
            e = ev['end'].get('dateTime')
            if s and e:
                try:
                    bs = datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
                    be = datetime.datetime.fromisoformat(e.replace('Z', '+00:00')).replace(tzinfo=None)
                    busy.append((bs, be))
                except Exception:
                    pass
        busy.sort()

        cursor = day_start
        for bs, be in busy:
            gap = (bs - cursor).total_seconds() / 60
            if gap >= duration_minutes:
                slots.append({
                    "start": cursor,
                    "end": cursor + datetime.timedelta(minutes=duration_minutes),
                    "date_str": cursor.strftime("%A, %B %d"),
                    "time_str": cursor.strftime("%I:%M %p")
                })
            cursor = max(cursor, be)
            if len(slots) >= 3:
                return slots

        # Gap after last event
        if (day_end - cursor).total_seconds() / 60 >= duration_minutes:
            slots.append({
                "start": cursor,
                "end": cursor + datetime.timedelta(minutes=duration_minutes),
                "date_str": cursor.strftime("%A, %B %d"),
                "time_str": cursor.strftime("%I:%M %p")
            })

        if len(slots) >= 3:
            return slots

    return slots


def handle_find_a_time(team_id: str, user_id: str, user_message: str) -> str:
    """
    Parse a natural-language 'find a time' request, search the user's calendar
    for free slots, and return a formatted list of options.
    """
    # Extract duration and attendee info with Claude
    try:
        parse_resp = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f'Extract from this message: "{user_message}"\n'
                    "Return ONLY JSON with: duration_minutes (int, default 60), "
                    "attendee (string name or email or null), purpose (string or null)"
                )
            }]
        )
        raw = parse_resp.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        details = json.loads(raw)
        duration = int(details.get('duration_minutes') or 60)
        attendee = details.get('attendee') or ''
        purpose  = details.get('purpose') or 'meeting'
    except Exception:
        duration = 60
        attendee = ''
        purpose  = 'meeting'

    slots = find_free_slots(team_id, user_id, duration_minutes=duration)
    if not slots:
        return (
            f"I couldn't find a free {duration}-minute window in the next 5 business days. "
            f"Your calendar looks fully booked — want me to check further out?"
        )

    lines = [f"🗓️ Here are your next available {duration}-minute slots:\n"]
    for i, slot in enumerate(slots, 1):
        lines.append(f"*Option {i}:* {slot['date_str']} at {slot['time_str']}")

    with_str = f" with {attendee}" if attendee else ""
    lines.append(
        f"\nReply *book option 1*, *book option 2*, or *book option 3* to schedule "
        f"your {purpose}{with_str} — I'll create the calendar event."
    )
    return "\n".join(lines)


def handle_book_option(team_id: str, user_id: str, option_num: int,
                       attendee_email: str | None = None) -> str:
    """Book the Nth free slot from the most recent find_free_slots call."""
    slots = find_free_slots(team_id, user_id, duration_minutes=60)
    if not slots or option_num > len(slots):
        return "I couldn't re-find that slot. Please run 'find a time' again."
    slot = slots[option_num - 1]
    result = create_calendar_event(
        team_id, user_id, "Meeting",
        slot["start"], 60,
        [attendee_email] if attendee_email else None
    )
    return f"✅ Done! Booked for *{slot['date_str']} at {slot['time_str']}*\n\n{result}"


def check_calendar_conflicts(team_id: str, user_id: str) -> str | None:
    """
    Check today's calendar for back-to-back meetings with no break or overlapping
    events. Returns a warning string if conflicts exist, else None.
    """
    service = get_calendar_service(team_id, user_id)
    if not service:
        return None

    now = datetime.datetime.utcnow()
    day_start = now.replace(hour=0,  minute=0, second=0, microsecond=0).isoformat() + 'Z'
    day_end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + 'Z'

    try:
        result = service.events().list(
            calendarId='primary', timeMin=day_start, timeMax=day_end,
            singleEvents=True, orderBy='startTime'
        ).execute()
    except Exception:
        return None

    events = [
        e for e in result.get('items', [])
        if e['start'].get('dateTime')
    ]

    warnings = []
    for i in range(len(events) - 1):
        try:
            end_curr  = datetime.datetime.fromisoformat(
                events[i]['end']['dateTime'].replace('Z', '+00:00')).replace(tzinfo=None)
            start_next = datetime.datetime.fromisoformat(
                events[i + 1]['start']['dateTime'].replace('Z', '+00:00')).replace(tzinfo=None)
            gap = (start_next - end_curr).total_seconds() / 60
            if gap < 0:
                warnings.append(
                    f"⚠️ *{events[i]['summary']}* overlaps with *{events[i+1]['summary']}*"
                )
            elif gap < 5:
                warnings.append(
                    f"⚠️ No break between *{events[i]['summary']}* and *{events[i+1]['summary']}*"
                )
        except Exception:
            pass

    return "\n".join(warnings) if warnings else None


def get_user_timezone(team_id: str, user_id: str) -> str:
    """
    Get stored timezone for a user, defaulting to Africa/Lagos.

    Args:
        team_id (str): Slack workspace ID.
        user_id (str): Slack user ID.

    Returns:
        str: IANA timezone string (e.g. 'America/New_York').
    """
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT timezone FROM user_timezones WHERE team_id = ? AND user_id = ?",
        (team_id, user_id)
    ).fetchone()
    conn.close()
    return row[0] if row else 'Africa/Lagos'


def set_user_timezone(team_id: str, user_id: str, timezone: str) -> None:
    """
    Store or update a user's timezone preference.

    Args:
        team_id (str): Slack workspace ID.
        user_id (str): Slack user ID.
        timezone (str): IANA timezone string (e.g. 'America/New_York').
    """
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        """INSERT OR REPLACE INTO user_timezones (team_id, user_id, timezone, updated_at)
           VALUES (?, ?, ?, ?)""",
        (team_id, user_id, timezone, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def store_google_token(team_id: str, user_id: str, creds) -> None:
    """
    Save a user's Google Calendar OAuth token to the database.

    The credentials object is serialised with pickle and stored as a blob
    so it survives deployments without needing a file on disk per user.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID (individual user within the workspace).
        creds: A google.oauth2.credentials.Credentials object.
    """
    token_data = pickle.dumps(creds)
    conn = sqlite3.connect("data/bot.db")
    conn.execute(
        "INSERT OR REPLACE INTO google_tokens (team_id, user_id, token_data, updated_at) VALUES (?, ?, ?, ?)",
        (team_id, user_id, token_data, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_google_token(team_id: str, user_id: str):
    """
    Load the stored Google Calendar credentials for a specific user.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID.

    Returns:
        google.oauth2.credentials.Credentials | None: The credentials object,
        or None if this user hasn't connected Google Calendar yet.
    """
    conn = sqlite3.connect("data/bot.db")
    row = conn.execute(
        "SELECT token_data FROM google_tokens WHERE team_id = ? AND user_id = ?",
        (team_id, user_id)
    ).fetchone()
    conn.close()
    if row:
        return pickle.loads(row[0])
    return None


# ---------------------------------------------------------------------------
# Google Calendar Helpers
# ---------------------------------------------------------------------------

# Path for Google credentials file — loaded from GOOGLE_CREDENTIALS_JSON env var
CREDENTIALS_PATH = "data/credentials.json"


def load_google_credentials_file() -> str | None:
    """
    Ensure credentials.json is available on disk.

    On Railway, the file content is stored in the GOOGLE_CREDENTIALS_JSON
    environment variable and written to the data/ Volume directory on startup.
    Locally, the file is read directly from credentials.json.

    Returns:
        str | None: Path to the credentials file, or None if not found.
    """
    # Write from env var if available (Railway production)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        os.makedirs("data", exist_ok=True)
        with open(CREDENTIALS_PATH, 'w') as f:
            f.write(creds_json)
        return CREDENTIALS_PATH

    # Fall back to local file for development
    if os.path.exists('credentials.json'):
        return 'credentials.json'

    return None


def get_calendar_service(team_id: str, user_id: str):
    """
    Return an authorised Google Calendar API client for a specific user.

    Loads the stored token for this (team_id, user_id) pair from the database.
    If the token is expired, it is refreshed silently and saved back. If no
    token exists, returns None — the bot will prompt the user to visit /auth/google.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID (individual user within the workspace).

    Returns:
        googleapiclient.discovery.Resource | None: Authorized Google Calendar
        API service for this user, or None if not yet authenticated.
    """
    creds = get_google_token(team_id, user_id)

    if not creds:
        return None

    # Refresh silently if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            store_google_token(team_id, user_id, creds)
        except Exception as e:
            print(f"Token refresh failed for user {user_id} in team {team_id}: {e}")
            return None

    if not creds.valid:
        return None

    return build('calendar', 'v3', credentials=creds)


def build_calendar_auth_link(team_id: str, user_id: str) -> str:
    """
    Build the personalised Google Calendar OAuth link for a user.

    The link is derived from SLACK_REDIRECT_URI so it always points to the
    correct Railway URL without needing a separate BASE_URL variable.

    Args:
        team_id (str): Slack workspace/team ID.
        user_id (str): Slack user ID.

    Returns:
        str: Full URL the user should visit to connect their Google Calendar.
    """
    slack_redirect = os.environ.get("SLACK_REDIRECT_URI", "")
    base_url = slack_redirect.rsplit("/slack/oauth_redirect", 1)[0]
    return f"{base_url}/auth/google?team_id={team_id}&user_id={user_id}"


def get_events_for_date(team_id: str, user_id: str, days_offset: int = 0) -> str:
    """
    Fetch all Google Calendar events for a given day and return them as a
    formatted string.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID whose calendar to query.
        days_offset (int): Number of days from today.
                           0 = today, 1 = tomorrow, 2 = day after, etc.

    Returns:
        str: A newline-separated list of events (e.g. "- Team Standup at 09:00"),
             or a friendly message if no events are found, or an error string
             if the API call fails.
    """
    try:
        service = get_calendar_service(team_id, user_id)
        if not service:
            auth_link = build_calendar_auth_link(team_id, user_id)
            return f"📅 Google Calendar not connected. Visit {auth_link} to connect."
        now = datetime.datetime.utcnow()
        target_date = now + datetime.timedelta(days=days_offset)

        # Build UTC time boundaries for the full target day
        day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + 'Z'
        day_end = target_date.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=day_start,
            timeMax=day_end,
            singleEvents=True,   # Expand recurring events into individual instances
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        if not events:
            day_name = "today" if days_offset == 0 else "tomorrow" if days_offset == 1 else f"in {days_offset} days"
            return f"No events scheduled for {day_name}."

        # Format each event as "- Title at start_time"
        event_list = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            event_list.append(f"- {event['summary']} at {start}")

        return "\n".join(event_list)

    except Exception as e:
        return f"Could not fetch calendar: {str(e)}"


def create_calendar_event(
    team_id: str,
    user_id: str,
    summary: str,
    start_time: datetime.datetime,
    duration_minutes: int = 60,
    attendee_emails: list[str] | None = None
) -> str:
    """
    Create a new Google Calendar event and optionally invite attendees.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID whose calendar to create the event in.
        summary (str): Title/name of the event.
        start_time (datetime.datetime): Event start time (naive datetime, assumed Africa/Lagos).
        duration_minutes (int): Length of the event in minutes. Defaults to 60.
        attendee_emails (list[str] | None): List of email addresses to invite.
                                            Pass None or empty list for no attendees.

    Returns:
        str: Confirmation message with event title, time, calendar link, and
             list of invited attendees (if any), or an error string on failure.
    """
    try:
        service = get_calendar_service(team_id, user_id)
        if not service:
            auth_link = build_calendar_auth_link(team_id, user_id)
            return f"📅 Google Calendar not connected. Visit {auth_link} to connect."
        end_time = start_time + datetime.timedelta(minutes=duration_minutes)

        event = {
            'summary': summary,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'Africa/Lagos',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'Africa/Lagos',
            },
        }

        # Attach attendees if provided — Google will send them calendar invites
        if attendee_emails:
            event['attendees'] = [{'email': email} for email in attendee_emails]
            event['sendUpdates'] = 'all'

        event = service.events().insert(
            calendarId='primary',
            body=event,
            sendUpdates='all'
        ).execute()

        response_text = (
            f"Event created: {event.get('summary')} at {event['start'].get('dateTime')}\n"
            f"Link: {event.get('htmlLink')}"
        )

        if attendee_emails:
            response_text += f"\n\nInvitations sent to: {', '.join(attendee_emails)}"

        return response_text

    except Exception as e:
        return f"Could not create event: {str(e)}"


def delete_calendar_event(team_id: str, user_id: str, event_title: str, days_offset: int = 0) -> str:
    """
    Find and delete a calendar event by partial title match on a given day.

    The search is case-insensitive and matches any event whose summary contains
    the provided title string. If multiple matches are found, the user is asked
    to be more specific rather than deleting all of them.

    Args:
        team_id (str): The Slack workspace/team ID.
        user_id (str): The Slack user ID whose calendar to search.
        event_title (str): Partial or full title of the event to delete.
        days_offset (int): 0 = today, 1 = tomorrow, etc. Defaults to 0.

    Returns:
        str: Confirmation of deletion, a prompt to be more specific if multiple
             events match, a not-found message, or an error string on failure.
    """
    try:
        service = get_calendar_service(team_id, user_id)
        if not service:
            auth_link = build_calendar_auth_link(team_id, user_id)
            return f"📅 Google Calendar not connected. Visit {auth_link} to connect."
        now = datetime.datetime.utcnow()
        target_date = now + datetime.timedelta(days=days_offset)

        day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + 'Z'
        day_end = target_date.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=day_start,
            timeMax=day_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        # Filter events whose summary contains the search string (case-insensitive)
        matching_events = [e for e in events if event_title.lower() in e['summary'].lower()]

        if not matching_events:
            return f"No events found matching '{event_title}'"

        # Require exact enough match to avoid accidental bulk deletion
        if len(matching_events) > 1:
            event_list = "\n".join([
                f"- {e['summary']} at {e['start'].get('dateTime', e['start'].get('date'))}"
                for e in matching_events
            ])
            return f"Found multiple events matching '{event_title}':\n{event_list}\n\nPlease be more specific."

        event_to_delete = matching_events[0]
        service.events().delete(
            calendarId='primary',
            eventId=event_to_delete['id'],
            sendUpdates='all'   # Notify attendees that the event was cancelled
        ).execute()

        return (
            f"Deleted event: {event_to_delete['summary']} at "
            f"{event_to_delete['start'].get('dateTime', event_to_delete['start'].get('date'))}\n\n"
            f"Cancellation emails sent to attendees."
        )

    except Exception as e:
        return f"Could not delete event: {str(e)}"


# ---------------------------------------------------------------------------
# Conversation History (Context-Aware Replies)
# ---------------------------------------------------------------------------

def get_user_history(team_id: str, user_id: str) -> list:
    """
    Retrieve the recent DM conversation history for a user.

    Used to provide multi-turn context to the AI so it remembers what was
    discussed earlier in the same conversation session.

    Args:
        team_id (str): Slack workspace ID.
        user_id (str): Slack user ID.

    Returns:
        list: List of {"role": "user"|"assistant", "content": str} dicts.
    """
    return conversation_history.get(f"{team_id}:{user_id}", [])


def update_user_history(team_id: str, user_id: str, role: str, content: str) -> None:
    """
    Append a message to a user's conversation history, capping at 10 entries.

    Args:
        team_id (str): Slack workspace ID.
        user_id (str): Slack user ID.
        role (str): 'user' or 'assistant'.
        content (str): The message text.
    """
    key = f"{team_id}:{user_id}"
    if key not in conversation_history:
        conversation_history[key] = []
    conversation_history[key].append({"role": role, "content": content})
    # Keep only the last 10 messages to stay within token limits
    if len(conversation_history[key]) > 10:
        conversation_history[key] = conversation_history[key][-10:]


# ---------------------------------------------------------------------------
# Channel & Thread Summarization
# ---------------------------------------------------------------------------

def get_channel_id(channel_name: str, bot_token: str) -> str | None:
    """
    Look up a Slack channel ID by name.

    Searches both public and private channels the bot has access to.

    Args:
        channel_name (str): Channel name with or without the # prefix.
        bot_token (str): Bot token for the workspace to search in.

    Returns:
        str | None: The Slack channel ID (e.g. 'C01234567'), or None if not found.
    """
    channel_name = channel_name.lstrip('#').lower()
    client = WebClient(token=bot_token)
    try:
        for channel_type in ["public_channel", "private_channel"]:
            cursor = None
            while True:
                result = client.conversations_list(
                    types=channel_type,
                    limit=200,
                    cursor=cursor
                )
                for channel in result.get('channels', []):
                    if channel['name'].lower() == channel_name:
                        return channel['id']
                cursor = result.get('response_metadata', {}).get('next_cursor')
                if not cursor:
                    break
    except Exception as e:
        print(f"Error finding channel '{channel_name}': {e}")
    return None


def resolve_user_names(user_ids: list, bot_token: str) -> dict:
    """
    Resolve a list of Slack user IDs to display names.

    Args:
        user_ids (list): List of Slack user ID strings.
        bot_token (str): Bot token for the workspace.

    Returns:
        dict: Maps user_id -> display name string.
    """
    client = WebClient(token=bot_token)
    names = {}
    for uid in set(user_ids):
        try:
            info = client.users_info(user=uid)
            profile = info['user']['profile']
            names[uid] = profile.get('display_name') or profile.get('real_name', uid)
        except Exception:
            names[uid] = uid
    return names


def summarize_channel_history(channel_id: str, bot_token: str, hours: int = 24) -> str:
    """
    Fetch and summarise recent messages from a Slack channel.

    Retrieves messages from the last `hours` hours, resolves user display
    names, then asks Claude to produce a concise summary with key topics,
    decisions, and action items.

    Args:
        channel_id (str): Slack channel ID.
        bot_token (str): Bot token for the workspace.
        hours (int): How many hours back to look. Defaults to 24.

    Returns:
        str: AI-generated summary, or an error/empty message.
    """
    client = WebClient(token=bot_token)
    try:
        oldest = str((datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).timestamp())
        result = client.conversations_history(channel=channel_id, oldest=oldest, limit=200)
        messages = [m for m in result.get('messages', []) if m.get('text') and not m.get('bot_id')]

        if not messages:
            return "No messages found in that channel for the past 24 hours."

        # Resolve user IDs to names for a more readable summary
        user_ids = [m.get('user') for m in messages if m.get('user')]
        names = resolve_user_names(user_ids, bot_token)

        formatted = "\n".join([
            f"{names.get(m.get('user', ''), 'Unknown')}: {m.get('text', '')}"
            for m in reversed(messages)
        ])

        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this Slack channel conversation from the past 24 hours.\n"
                    f"Structure your summary as:\n"
                    f"📌 **Key Topics**: (bullet list)\n"
                    f"✅ **Decisions Made**: (bullet list, or 'None' if none)\n"
                    f"📋 **Action Items**: (bullet list, or 'None' if none)\n"
                    f"❓ **Open Questions**: (bullet list, or 'None' if none)\n\n"
                    f"Conversation:\n{formatted}"
                )
            }]
        )
        return response.content[0].text

    except Exception as e:
        return f"Could not summarize channel: {str(e)}"


def summarize_thread(channel_id: str, thread_ts: str, bot_token: str) -> str:
    """
    Fetch and summarise all messages in a Slack thread.

    Retrieves all replies, resolves user names, then asks Claude to extract
    decisions, action items, and open questions from the thread.

    Args:
        channel_id (str): Slack channel ID containing the thread.
        thread_ts (str): Timestamp of the thread root message.
        bot_token (str): Bot token for the workspace.

    Returns:
        str: AI-generated thread summary with structured output.
    """
    client = WebClient(token=bot_token)
    try:
        result = client.conversations_replies(channel=channel_id, ts=thread_ts)
        messages = [m for m in result.get('messages', []) if m.get('text')]

        if len(messages) < 2:
            return "Not enough messages in this thread to summarize yet."

        user_ids = [m.get('user') for m in messages if m.get('user')]
        names = resolve_user_names(user_ids, bot_token)

        formatted = "\n".join([
            f"{names.get(m.get('user', ''), 'Bot')}: {m.get('text', '')}"
            for m in messages
        ])

        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this Slack thread ({len(messages)} messages).\n"
                    f"Structure your output as:\n"
                    f"📌 **Summary**: (2-3 sentence overview)\n"
                    f"✅ **Decisions**: (bullet list, or 'None')\n"
                    f"📋 **Action Items**: (bullet list with owners if mentioned, or 'None')\n"
                    f"❓ **Open Questions**: (bullet list, or 'None')\n\n"
                    f"Thread:\n{formatted}"
                )
            }]
        )
        return response.content[0].text

    except Exception as e:
        return f"Could not summarize thread: {str(e)}"


# ---------------------------------------------------------------------------
# Timezone Intelligence
# ---------------------------------------------------------------------------

TIMEZONE_ABBREVIATIONS: dict[str, str] = {
    "EST": "America/New_York",   "EDT": "America/New_York",
    "CST": "America/Chicago",    "CDT": "America/Chicago",
    "MST": "America/Denver",     "MDT": "America/Denver",
    "PST": "America/Los_Angeles","PDT": "America/Los_Angeles",
    "GMT": "Europe/London",      "BST": "Europe/London",
    "CET": "Europe/Paris",       "CEST": "Europe/Paris",
    "EET": "Europe/Helsinki",    "EEST": "Europe/Helsinki",
    "IST": "Asia/Kolkata",
    "JST": "Asia/Tokyo",         "KST": "Asia/Seoul",
    "CST8": "Asia/Shanghai",     "HKT": "Asia/Hong_Kong",
    "SGT": "Asia/Singapore",     "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",  "NZST": "Pacific/Auckland",
    "WAT": "Africa/Lagos",       "CAT": "Africa/Harare",
    "EAT": "Africa/Nairobi",     "SAST": "Africa/Johannesburg",
    "UTC": "UTC",                "Z": "UTC",
}

TIME_MENTION_PATTERN = re.compile(
    r'\b(\d{1,2}):?(\d{2})?\s*(am|pm|AM|PM)?\s*'
    r'(UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT|BST|CET|CEST|EET|EEST|'
    r'IST|JST|KST|SGT|HKT|AEST|AEDT|NZST|WAT|CAT|EAT|SAST|Z)\b',
    re.IGNORECASE,
)

CHANNEL_SUMMARY_PATTERN = re.compile(
    r'(?:summarize|summary|what(?:\'s| is)(?: happening| going on)?'
    r'|catch me up|recap|tldr|what did i miss)'
    r'.*?(?:<#([A-Z0-9]+)(?:\|[^>]+)?>|#(\w[\w-]*))',
    re.IGNORECASE,
)

def detect_and_convert_times(text: str, user_timezone: str) -> str | None:
    """
    Detect time+timezone mentions in text and convert them to the user's timezone.

    Also calculates overlap windows and flags times outside standard business hours.

    Args:
        text (str): The message text to scan for time mentions.
        user_timezone (str): IANA timezone for the user (e.g. 'Africa/Lagos').

    Returns:
        str | None: A formatted timezone conversion block, or None if no times found.
    """
    matches = TIME_MENTION_PATTERN.findall(text)
    if not matches:
        return None

    conversions = []
    user_tz = pytz.timezone(user_timezone)

    for hour_str, minute_str, ampm, tz_abbr in matches:
        try:
            hour = int(hour_str)
            minute = int(minute_str) if minute_str else 0

            if ampm.lower() == 'pm' and hour != 12:
                hour += 12
            elif ampm.lower() == 'am' and hour == 12:
                hour = 0

            source_tz_name = TIMEZONE_ABBREVIATIONS.get(tz_abbr.upper(), 'UTC')
            source_tz = pytz.timezone(source_tz_name)

            # Build a timezone-aware datetime for today at the given time
            today = datetime.datetime.now(source_tz)
            source_time = source_tz.localize(
                datetime.datetime(today.year, today.month, today.day, hour, minute)
            )

            user_time = source_time.astimezone(user_tz)
            utc_time = source_time.astimezone(pytz.utc)

            # Flag times outside 9am-6pm in either timezone
            flags = []
            if not (9 <= source_time.hour < 18):
                flags.append(f"⚠️ outside business hours in {tz_abbr}")
            if not (9 <= user_time.hour < 18):
                flags.append("⚠️ outside your business hours")

            flag_str = f"  {', '.join(flags)}" if flags else ""

            original = f"{hour_str}:{minute_str or '00'} {ampm.upper()} {tz_abbr.upper()}"
            conversions.append(
                f"• *{original}* → *{user_time.strftime('%I:%M %p')} your time* "
                f"({utc_time.strftime('%H:%M UTC')}){flag_str}"
            )

        except Exception as e:
            print(f"Timezone conversion error: {e}")

    if conversions:
        return "🕒 *Time Conversion:*\n" + "\n".join(conversions)
    return None


# ---------------------------------------------------------------------------
# Slack Utility Helpers
# ---------------------------------------------------------------------------

def check_user_active(user_id: str, bot_token: str) -> bool:
    """
    Check whether a workspace owner is currently marked as 'active' on Slack.

    This is used by the auto-responder to skip sending a reply if the owner
    appears to be online (they may just be slow to respond).

    Args:
        user_id (str): The Slack user ID of the workspace owner to check.
        bot_token (str): The bot token for the workspace where the check is needed.

    Returns:
        bool: True if the owner's Slack presence is 'active', False otherwise
              or if the presence check fails.
    """
    try:
        client = WebClient(token=bot_token)
        response = client.users_getPresence(user=user_id)
        return response['presence'] == 'active'
    except Exception as e:
        print(f"Error checking presence: {e}")
    return False


def search_slack_history(query: str, bot_token: str) -> list:
    """
    Search Slack message history for messages relevant to a given query.

    Results are used to give the AI assistant context about past conversations
    before generating an auto-reply.

    Args:
        query (str): The search string (typically the first 100 chars of the mention).
        bot_token (str): The bot token for the workspace to search in.

    Returns:
        list: Up to 3 matching Slack message objects, or an empty list on failure.
    """
    try:
        client = WebClient(token=bot_token)
        result = client.search_messages(query=query, count=5)
        if result['messages']['matches']:
            return result['messages']['matches'][:3]
    except Exception as e:
        print(f"Error searching history: {e}")
    return []


# ---------------------------------------------------------------------------
# Auto-Reply Logic
# ---------------------------------------------------------------------------

def auto_respond_to_mention(
    team_id: str,
    channel: str,
    thread_ts: str,
    original_message: str,
    owner_user_id: str,
    bot_token: str,
    owner_name: str = "the owner"
) -> None:
    """
    Wait 5 minutes after a channel mention, then auto-reply on the owner's behalf
    if they haven't responded yet.

    This function is always run in a background daemon thread (one per mention).
    After sleeping, it checks several bail-out conditions before posting:
      1. The mention was already handled (removed from pending_mentions).
      2. The owner is currently active on Slack.
      3. The message contains sensitive keywords.

    If none of the bail-out conditions apply, it queries the Anthropic API with
    context from Slack history and posts a reply in the original thread.

    Args:
        team_id (str): Slack workspace/team ID, used to scope the pending_mentions key.
        channel (str): Slack channel ID where the mention occurred.
        thread_ts (str): Timestamp of the thread root message (used as thread identifier).
        original_message (str): Full text of the message that mentioned the owner.
        owner_user_id (str): Slack user ID of the workspace owner being mentioned.
        bot_token (str): Bot token for this workspace, used to post the auto-reply.
        owner_name (str): Display name of the owner, used to personalise the reply.
    """
    # Wait the full 5-minute window before doing anything
    time.sleep(300)

    key = f"{team_id}:{channel}:{thread_ts}"

    # Bail out if the owner already responded (key removed from pending_mentions)
    if key not in pending_mentions:
        return

    # Bail out if the message contains sensitive/private keywords
    message_lower = original_message.lower()
    if any(keyword in message_lower for keyword in SENSITIVE_KEYWORDS):
        print(f"Sensitive content detected, skipping auto-response for {key}")
        del pending_mentions[key]
        return

    try:
        # Search for relevant past messages to give the AI helpful context
        search_results = search_slack_history(original_message[:100], bot_token)

        context = f"Someone asked: '{original_message}'\n"
        if search_results:
            context += "\nRelevant past messages:\n"
            for msg in search_results:
                context += f"- {msg.get('text', '')[:200]}\n"

        ai_prompt = f"""{context}

You are {owner_name}'s AI assistant. {owner_name} hasn't responded yet. Provide a helpful response that:
1. Acknowledges you're the assistant
2. Provides useful information if possible
3. Asks clarifying questions if needed
4. Mentions {owner_name} will follow up

Keep it brief and professional."""

        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": ai_prompt}]
        )

        reply = (
            f"👋 Hi! I'm {owner_name}'s AI assistant. They haven't responded yet, but let me help:\n\n"
            f"{response.content[0].text}\n\n"
            f"_{owner_name} will follow up when available._"
        )

        # Post the AI reply in the same thread using this workspace's bot token
        client = WebClient(token=bot_token)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=reply
        )

        print(f"Auto-responded to mention in {channel} (workspace: {team_id})")

    except Exception as e:
        print(f"Error in auto-response: {e}")

    # Clean up the pending mention regardless of success or failure
    if key in pending_mentions:
        del pending_mentions[key]


# ---------------------------------------------------------------------------
# Scheduled Jobs
# ---------------------------------------------------------------------------

def send_daily_standup() -> None:
    """
    Send the daily standup prompt to every registered workspace owner as a Slack DM.

    Iterates over all workspaces stored in the database. For each workspace, it
    looks up the installed bot token, fetches today's Google Calendar events, and
    sends a personalised morning message to the workspace owner.

    This function is registered with the `schedule` library and fires every
    day at 09:00 (in whatever timezone the host machine is set to).
    """
    workspaces = get_all_workspaces()

    if not workspaces:
        print("No workspaces registered yet, skipping standup.")
        return

    for team_id, user_id in workspaces:
        try:
            # Look up the stored bot token for this workspace
            bot_token = get_installation_token(team_id)
            if not bot_token:
                print(f"No installation found for team {team_id}, skipping.")
                continue

            client = WebClient(token=bot_token)
            calendar_info = get_events_for_date(team_id, user_id, 0)

            # Check for calendar conflicts and append a warning if found
            conflict_warn = check_calendar_conflicts(team_id, user_id)
            conflict_section = f"\n\n⚠️ *Scheduling conflicts today:*\n{conflict_warn}" if conflict_warn else ""

            # Fetch pending action items carried over from previous days
            all_pending = get_pending_action_items(team_id, user_id)
            old_items = [i for i in all_pending
                         if i['created_at'][:10] < datetime.date.today().isoformat()]
            carryover = ""
            if old_items:
                task_list = "\n".join(f"• {i['task']}" for i in old_items[:5])
                carryover = f"\n\n📌 *Carried over from yesterday:*\n{task_list}"

            # Fetch Jira assigned issues for this user (if Jira is connected)
            jira_section = ""
            if jira_available():
                memories   = get_user_memories(team_id, user_id)
                jira_email = memories.get("jira_email")
                jira_issues = get_my_jira_issues(jira_email)
                # Only include if there are actual issues (skip the "not connected" warning)
                if not jira_issues.startswith("⚠️") and not jira_issues.startswith("✅ No open"):
                    jira_section = f"\n\n{jira_issues}"

            message = (
                f"Good morning! What are you working on today?\n\n"
                f"📅 *Your calendar:*\n{calendar_info}"
                f"{conflict_section}"
                f"{carryover}"
                f"{jira_section}"
            )
            client.chat_postMessage(channel=user_id, text=message)
            mark_standup_sent(team_id, user_id)
            print(f"Daily standup sent to {user_id} in workspace {team_id}")

        except Exception as e:
            print(f"Error sending standup to workspace {team_id}: {e}")


# Register all scheduled jobs
schedule.every().day.at("09:00").do(send_daily_standup)           # Morning standup
schedule.every(5).minutes.do(check_and_send_meeting_briefings)    # Pre-meeting briefings
schedule.every().day.at("17:00").do(send_eod_followup)            # End-of-day check-in
schedule.every().friday.at("17:00").do(send_weekly_retro)         # Weekly retrospective


def run_scheduler() -> None:
    """
    Run the schedule loop in a background thread.

    Checks every 60 seconds whether any scheduled jobs are due and executes
    them. This runs as a daemon thread so it exits automatically when the
    main process stops.
    """
    while True:
        schedule.run_pending()
        time.sleep(60)


# ---------------------------------------------------------------------------
# DM Handler
# ---------------------------------------------------------------------------

def process_direct_message(event: dict, say) -> None:
    """
    Handle an incoming direct message from the bot owner.

    Parses the message for three types of requests (in priority order):
      1. **Delete/cancel event** — extracts event title and date via AI, then deletes it.
      2. **Schedule event** — extracts event details via AI, then creates it.
      3. **General query** — fetches relevant calendar context if needed, then
         passes everything to the AI for a conversational response.

    Also registers the sender as the workspace owner on their first DM if no
    owner has been recorded for this workspace yet.

    Args:
        event (dict): The Slack event payload for the incoming message.
        say (callable): Slack Bolt's `say` function to reply in the same channel/DM.
    """
    user = event.get('user')
    team_id = event.get('team')
    user_message = event.get('text', '')
    user_message_lower = user_message.lower()

    # Register the sender as workspace owner on their very first DM
    if team_id and not get_workspace_owner(team_id):
        set_workspace_owner(team_id, user)
        print(f"Workspace owner set: user {user} in team {team_id}")

    # If this user hasn't connected their Google Calendar yet, prompt them.
    # We do this for every user (not just the workspace owner) so anyone who
    # DMs the bot can get full calendar features.
    if team_id and user and not get_google_token(team_id, user):
        auth_link = build_calendar_auth_link(team_id, user)
        say(
            f"👋 Welcome! I'm your Standup Agent.\n\n"
            f"To unlock calendar features (daily standup, scheduling, and more), "
            f"connect *your* Google Calendar:\n"
            f"{auth_link}\n\n"
            f"You can still chat with me without connecting — just DM me anything!"
        )
        # Don't return — still process the message so the bot responds normally

    print(f"Processing DM: {user_message[:50]}...")

    # --- EOD "done" shortcut: user marks today's action items as complete ---
    if user_message_lower.strip() in ('done', 'all done', 'yes all done', 'finished', 'completed'):
        count = mark_all_todays_items_done(team_id, user)
        if count:
            say(f"✅ Great work! Marked *{count} task{'s' if count != 1 else ''}* as done for today. 🎉")
            return
        # Fall through to normal processing if no tasks to mark

    # --- Action item query: "what are my tasks / action items?" ---
    if any(p in user_message_lower for p in [
        "my tasks", "action items", "my to-do", "my todo", "what do i have to do",
        "pending tasks", "open tasks", "what should i work on"
    ]):
        items = get_pending_action_items(team_id, user)
        if items:
            task_list = "\n".join(f"{i+1}. {item['task']}" for i, item in enumerate(items))
            say(f"📋 *Your pending tasks ({len(items)} total):*\n\n{task_list}\n\nReply *done* to mark today's tasks complete.")
        else:
            say("✅ You have no pending tasks right now. You're all caught up!")
        return

    # --- Memory query: "what was I working on last week/month?" ---
    if any(p in user_message_lower for p in [
        "what was i working on", "what did i work on", "what have i been doing",
        "my history", "last week", "last month", "past week", "recent work"
    ]):
        days = 30 if any(w in user_message_lower for w in ["month", "30"]) else 7
        history = get_standup_history(team_id, user, days=days)
        if history:
            summary = "\n".join(f"*{e['date']}:* {e['response'][:120]}" for e in history[:10])
            say(f"🗂️ *Your work history (last {days} days):*\n\n{summary}")
        else:
            say("I don't have any standup history for you yet. Reply to the morning standup each day and I'll track it.")
        return

    # --- Find a time: "find a time with X" / "check my availability" ---
    if any(p in user_message_lower for p in [
        "find a time", "find time", "check availability", "when am i free",
        "when are we free", "schedule a meeting with", "find a slot"
    ]):
        say("🔍 Checking your calendar for free slots...")
        result = handle_find_a_time(team_id, user, user_message)
        say(result)
        return

    # --- Book an option from a previous find-a-time ---
    book_match = re.match(r'book\s+option\s+([123])', user_message_lower.strip())
    if book_match:
        option_num = int(book_match.group(1))
        say(f"📅 Booking option {option_num}...")
        result = handle_book_option(team_id, user, option_num)
        say(result)
        return

    # --- Jira: register email "my jira email is X" ---
    jira_email_match = re.search(
        r'(?:my jira email is|jira email[:\s]+|set jira email to)\s+([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        user_message, re.IGNORECASE
    )
    if jira_email_match:
        jira_email = jira_email_match.group(1)
        update_user_memory(team_id, user, "jira_email", jira_email)
        say(f"✅ Got it! I'll use *{jira_email}* to filter your Jira issues.")
        return

    # --- Jira: show my tickets ---
    if any(p in user_message_lower for p in [
        "my jira", "jira tickets", "jira issues", "jira tasks",
        "show jira", "open tickets", "open issues"
    ]):
        memories = get_user_memories(team_id, user)
        jira_email = memories.get("jira_email")
        say(get_my_jira_issues(jira_email))
        return

    # --- Jira: sprint progress ---
    if any(p in user_message_lower for p in [
        "sprint progress", "sprint status", "how's the sprint",
        "sprint update", "sprint report", "jira sprint"
    ]):
        say(get_sprint_progress())
        return

    # --- Jira: create issue ---
    # Patterns: "create jira bug: Login crash", "jira task: Add dark mode", "log a bug: X"
    jira_create_match = re.search(
        r'(?:create|add|log|new|open)\s+(?:a\s+)?(?:jira\s+)?'
        r'(bug|task|story|epic|subtask|improvement|feature)[:\s]+(.+)',
        user_message, re.IGNORECASE
    )
    if jira_create_match or 'create jira' in user_message_lower:
        if jira_create_match:
            raw_type = jira_create_match.group(1).strip().title()
            summary  = jira_create_match.group(2).strip()
            # Map friendly names to Jira issue type names
            type_map = {
                "Bug": "Bug", "Task": "Task", "Story": "Story",
                "Epic": "Epic", "Feature": "Story", "Improvement": "Task",
                "Subtask": "Sub-task",
            }
            issue_type = type_map.get(raw_type, "Task")
        else:
            # Generic "create jira X" — use Claude to extract details
            try:
                parse = anthropic.messages.create(
                    model="claude-sonnet-4-20250514", max_tokens=150,
                    messages=[{"role": "user", "content":
                        f'Extract from: "{user_message}"\nReturn JSON: {{"summary": "...", "issue_type": "Task|Bug|Story"}}'
                    }]
                )
                raw = parse.content[0].text.strip().replace('```json','').replace('```','').strip()
                details   = json.loads(raw)
                summary    = details.get("summary", user_message)
                issue_type = details.get("issue_type", "Task")
            except Exception:
                summary    = user_message
                issue_type = "Task"
        say(create_jira_issue(summary, issue_type=issue_type))
        return

    # --- Jira: update status  "mark PROJ-123 as done" / "close PROJ-123" ---
    jira_update_match = re.search(
        r'(?:mark|move|close|complete|transition|set)\s+'
        r'([A-Z]+-\d+)\s+(?:as\s+|to\s+)?(.+)',
        user_message, re.IGNORECASE
    )
    if not jira_update_match:
        # Also match "PROJ-123 is done" style
        jira_update_match = re.search(
            r'([A-Z]+-\d+)\s+(?:is\s+|to\s+)?(?:now\s+)?(done|closed|complete|in progress|to do|todo)',
            user_message, re.IGNORECASE
        )
    if jira_update_match:
        issue_key     = jira_update_match.group(1).upper()
        target_status = jira_update_match.group(2).strip()
        say(update_jira_issue_status(issue_key, target_status))
        return

    # --- Focus time blocker ---
    if any(p in user_message_lower for p in [
        "block focus time", "focus block", "protect focus", "block my calendar",
        "block some time", "deep work block", "no meeting block"
    ]):
        say("🎯 Finding the next available focus window...")
        # Extract requested duration (default 2 hours)
        dur_match = re.search(r'(\d+)\s*(?:hour|hr)', user_message_lower)
        duration = int(dur_match.group(1)) * 60 if dur_match else 120
        slots = find_free_slots(team_id, user, duration_minutes=duration, days_ahead=3)
        if not slots:
            say("Your calendar is packed for the next 3 days — no free windows found.")
        else:
            slot = slots[0]
            result = create_calendar_event(
                team_id, user, "🎯 Focus Time",
                slot["start"], duration, None
            )
            say(f"✅ Blocked *{duration // 60}h of focus time* on *{slot['date_str']} at {slot['time_str']}*\n\n{result}")
        return

    # --- Timezone setting: "my timezone is EST" / "set timezone to WAT" ---
    tz_set_match = re.search(
        r'(?:my timezone is|set (?:my )?timezone to|i(?:\'m| am) in)\s+([A-Za-z/_]+)',
        user_message, re.IGNORECASE
    )
    if tz_set_match:
        tz_input = tz_set_match.group(1).upper()
        tz_name = TIMEZONE_ABBREVIATIONS.get(tz_input)
        if not tz_name:
            # Try as full IANA name
            try:
                pytz.timezone(tz_set_match.group(1))
                tz_name = tz_set_match.group(1)
            except Exception:
                tz_name = None
        if tz_name:
            set_user_timezone(team_id, user, tz_name)
            say(f"✅ Got it! I've set your timezone to *{tz_name}*. I'll convert all meeting times for you.")
        else:
            say(f"I don't recognise that timezone. Try something like `WAT`, `EST`, `PST`, or a full IANA name like `Africa/Lagos`.")
        return

    # --- Channel summary: "summarize #dev-team" / "what happened in #general" ---
    channel_match = CHANNEL_SUMMARY_PATTERN.search(user_message)
    if channel_match:
        channel_name = channel_match.group(1)
        say(f"Give me a moment to summarize *#{channel_name}*... 🔍")
        bot_token = get_installation_token(team_id)
        channel_id = get_channel_id(channel_name, bot_token)
        if channel_id:
            summary = summarize_channel_history(channel_id, bot_token)
            say(f"📋 *Summary of #{channel_name} (last 24 hours):*\n\n{summary}")
        else:
            say(
                f"I couldn't find a channel named *#{channel_name}*.\n"
                f"Make sure:\n• The channel name is spelled correctly\n"
                f"• The bot has been added to that channel"
            )
        return

    calendar_info = ""
    days_offset = None

    # --- Branch 1: Event deletion ---
    # Triggered by delete/cancel/remove + meeting/event/call keywords
    if (
        any(w in user_message_lower for w in ["delete", "cancel", "remove"])
        and any(w in user_message_lower for w in ["meeting", "event", "call"])
    ):
        deletion_prompt = f"""Extract the event deletion details from this message: "{user_message}"

Return ONLY a JSON object with these fields:
- event_title: the name/partial name of the event to delete
- date_context: "today", "tomorrow", or null for today

Example: "Delete the Product Sync meeting tomorrow" -> {{"event_title": "Product Sync", "date_context": "tomorrow"}}"""

        deletion_response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": deletion_prompt}]
        )

        try:
            extracted_text = deletion_response.content[0].text.strip()
            # Strip markdown code fences if the model wrapped the JSON
            extracted_text = extracted_text.replace('```json', '').replace('```', '').strip()
            delete_details = json.loads(extracted_text)

            if delete_details.get('event_title'):
                days = 1 if delete_details.get('date_context') == 'tomorrow' else 0
                result = delete_calendar_event(team_id, user, delete_details['event_title'], days)
                say(result)
                return
            else:
                say("Please specify which event you want to delete.")
                return
        except Exception as e:
            say(f"I had trouble understanding the deletion request. Error: {str(e)}")
            return

    # --- Branch 2: Event creation ---
    # Triggered by "schedule" + meeting/call/event keywords
    if "schedule" in user_message_lower and any(w in user_message_lower for w in ["meeting", "call", "event"]):
        extraction_prompt = f"""Extract the event details from this message: "{user_message}"

Return ONLY a JSON object with these fields:
- title: the event name/summary
- date: YYYY-MM-DD format
- time: HH:MM format in 24-hour time
- duration: number of minutes (default 60)
- attendees: array of email addresses

Today's date is {datetime.datetime.now().strftime('%Y-%m-%d')}"""

        extraction_response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": extraction_prompt}]
        )

        try:
            extracted_text = extraction_response.content[0].text.strip()
            extracted_text = extracted_text.replace('```json', '').replace('```', '').strip()
            event_details = json.loads(extracted_text)

            if event_details.get('title') and event_details.get('date') and event_details.get('time'):
                event_datetime = datetime.datetime.strptime(
                    f"{event_details['date']} {event_details['time']}",
                    "%Y-%m-%d %H:%M"
                )
                duration = event_details.get('duration', 60)
                attendees = event_details.get('attendees', [])

                result = create_calendar_event(
                    team_id,
                    user,
                    event_details['title'],
                    event_datetime,
                    duration,
                    attendees if attendees else None
                )
                say(result)
                return
            else:
                say("I couldn't extract all the event details.")
                return
        except Exception as e:
            say(f"I had trouble understanding the scheduling request. Error: {str(e)}")
            return

    # --- Branch 3: General query with optional calendar context ---
    if "tomorrow" in user_message_lower:
        days_offset = 1
    elif any(w in user_message_lower for w in ["today", "calendar", "meeting"]):
        days_offset = 0

    if days_offset is not None:
        calendar_info = f"\n\nCalendar information:\n{get_events_for_date(team_id, user, days_offset)}"

    # Build messages with conversation history for multi-turn context
    history = get_user_history(team_id, user)
    full_message = user_message + calendar_info
    messages = history + [{"role": "user", "content": full_message}]

    # Inject long-term memory into the system prompt so the AI knows the user
    memory_context = build_memory_context(team_id, user)
    system_prompt = (
        "You are a smart personal assistant in Slack. You help with calendar management, "
        "scheduling, answering questions, and workspace productivity. Be concise and friendly. "
        "Remember context from earlier in the conversation.\n\n"
        f"{memory_context}"
    )

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system_prompt,
        messages=messages
    )

    reply = response.content[0].text

    # Append timezone conversion if message contains time+timezone mentions
    user_tz = get_user_timezone(team_id, user)
    tz_conversion = detect_and_convert_times(user_message, user_tz)
    if tz_conversion:
        reply = f"{reply}\n\n{tz_conversion}"

    # Store in conversation history for future context
    update_user_history(team_id, user, "user", user_message)
    update_user_history(team_id, user, "assistant", reply)

    say(reply)

    # --- Background async tasks (don't block the response) ---

    # If this looks like a standup reply, save it and extract action items
    if standup_sent_today(team_id, user) and len(user_message) > 30:
        save_standup_response(team_id, user, user_message)
        extract_action_items_async(team_id, user, user_message)

    # Extract and update long-term memories from this conversation
    convo_snapshot = f"User: {user_message}\nAssistant: {reply}"
    extract_and_update_memories_async(team_id, user, convo_snapshot)


# ---------------------------------------------------------------------------
# Bot Mention Handler
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_app_mention(event: dict, say) -> None:
    """
    Fires whenever someone @mentions the bot directly in a channel.

    This allows anyone in the channel to talk to the bot directly — asking
    questions, checking availability, or requesting information — and get an
    immediate AI-powered response in the same thread.

    The bot's own mention tag is stripped from the message before sending
    it to the AI so the model only sees the actual question.

    Args:
        event (dict): The Slack event payload containing the message details.
        say (callable): Slack Bolt's reply function, scoped to the event's channel.
    """
    text = event.get('text', '')
    thread_ts = event.get('thread_ts', event.get('ts'))
    user = event.get('user')

    # Strip the @BotName mention tag from the message so the AI only sees
    # the actual question (e.g. "<@U12345> are you here?" → "are you here?")
    clean_text = ' '.join(
        word for word in text.split()
        if not word.startswith('<@')
    ).strip()

    # Fall back to a default prompt if the message was only the mention tag
    if not clean_text:
        clean_text = "Someone mentioned you. Greet them and let them know what you can help with."

    print(f"Bot mentioned in channel by {user}: {clean_text[:50]}...")

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    f"You are Kingsley's AI assistant in a Slack channel. "
                    f"Someone just asked: '{clean_text}'. "
                    f"Respond helpfully and concisely. If you don't have enough context "
                    f"to fully answer, say so and offer to help further."
                )
            }]
        )

        say(
            text=response.content[0].text,
            thread_ts=thread_ts
        )

    except Exception as e:
        print(f"Error handling app mention: {e}")
        say(
            text="Sorry, I ran into an issue processing your message. Please try again.",
            thread_ts=thread_ts
        )


# ---------------------------------------------------------------------------
# Main Event Handler
# ---------------------------------------------------------------------------

@app.event("message")
def handle_message_event(event: dict, say) -> None:
    """
    Top-level Slack message event handler. Routes all incoming messages to
    the appropriate sub-handler based on message type.

    Routing logic:
      - Bot messages and non-standard subtypes (edits, deletions) are ignored.
      - DMs (channel_type == 'im') are forwarded to process_direct_message().
      - Channel messages are scanned for @mentions of the owner:
          * If found and sent by someone else, a background thread is started
            to auto-respond after 5 minutes if the owner doesn't reply first.
          * If the owner sent the message, any pending auto-reply for that
            thread is cancelled.

    Args:
        event (dict): The raw Slack event payload.
        say (callable): Slack Bolt's reply function, scoped to the event's channel.
    """
    # Ignore messages sent by bots (including this bot itself).
    # Slack marks bot-generated messages with a 'bot_id' field — checking this
    # is more reliable than checking 'subtype' alone, since Slack sometimes
    # omits the bot_message subtype for DM responses.
    if event.get('bot_id') or event.get('subtype') == 'bot_message':
        return

    # Ignore secondary event subtypes like message_changed, message_deleted, etc.
    if event.get('subtype') is not None:
        return

    # --- DM: delegate to the direct message handler ---
    if event.get('channel_type') == 'im':
        process_direct_message(event, say)
        return

    # --- Channel message: extract fields for mention tracking ---
    team_id = event.get('team')
    channel = event.get('channel')
    text = event.get('text', '')
    user = event.get('user')
    ts = event['ts']
    # Use thread_ts if this is a reply; otherwise the message itself is the thread root
    thread_ts = event.get('thread_ts', ts)

    # Look up the registered owner for this workspace
    owner_user_id = get_workspace_owner(team_id) if team_id else None

    # No owner registered yet — nothing to monitor
    if not owner_user_id:
        return

    # If someone else mentioned the owner, start the auto-reply countdown
    if f"<@{owner_user_id}>" in text and user != owner_user_id:
        key = f"{team_id}:{channel}:{thread_ts}"

        # Look up this workspace's bot token for posting the reply later
        bot_token = get_installation_token(team_id)

        if bot_token:
            # Fetch the owner's display name to personalise the auto-reply
            try:
                client = WebClient(token=bot_token)
                user_info = client.users_info(user=owner_user_id)
                owner_name = user_info['user']['profile'].get('display_name') or \
                             user_info['user']['profile'].get('real_name', 'the owner')
            except Exception:
                owner_name = "the owner"

            # Register this mention as pending
            pending_mentions[key] = {
                'team_id': team_id,
                'channel': channel,
                'thread_ts': thread_ts,
                'message': text,
                'timestamp': time.time()
            }

            # Spin up a daemon thread — it will wait 5 min then auto-reply if needed
            threading.Thread(
                target=auto_respond_to_mention,
                args=(team_id, channel, thread_ts, text, owner_user_id, bot_token, owner_name),
                daemon=True
            ).start()

            print(f"Tracking mention in {channel} (team {team_id}), auto-reply in 5 min if no response")

    # If the owner replied in a thread with a pending mention, cancel the auto-reply
    if user == owner_user_id:
        key = f"{team_id}:{channel}:{thread_ts}"
        if key in pending_mentions:
            print(f"Owner responded, canceling auto-response for {key}")
            del pending_mentions[key]

    # --- Thread auto-summarization at 10+ replies ---
    # Track reply counts in memory; when a thread hits 10 messages auto-post a summary
    if thread_ts != ts:  # This message is a reply (not the root)
        count_key = f"{team_id}:{channel}:{thread_ts}"
        thread_reply_counts[count_key] = thread_reply_counts.get(count_key, 0) + 1

        if thread_reply_counts[count_key] == 10:
            bot_token = get_installation_token(team_id)
            if bot_token:
                def post_thread_summary(ch=channel, ts=thread_ts, tk=bot_token):
                    summary = summarize_thread(ch, ts, tk)
                    client = WebClient(token=tk)
                    client.chat_postMessage(
                        channel=ch,
                        thread_ts=ts,
                        text=f"🤖 *Auto-summary (10 messages reached):*\n\n{summary}"
                    )
                threading.Thread(target=post_thread_summary, daemon=True).start()
                print(f"Auto-summarizing thread {thread_ts} in {channel} (10 replies reached)")


# ---------------------------------------------------------------------------
# Slash Command Handler
# ---------------------------------------------------------------------------

@app.command("/summarize")
def handle_summarize_command(ack, command, say, client) -> None:
    """
    Handle the /summarize slash command.

    When used inside a thread: summarizes that specific thread.
    When used in a channel (not a thread): summarizes the last 24 hours
    of that channel's conversation.

    Usage:
      /summarize              — summarize current thread or channel
      /summarize #other-channel — summarize a specific channel

    Args:
        ack: Slack's acknowledgement function (must be called within 3 seconds).
        command (dict): The slash command payload from Slack.
        say (callable): Function to post a message in the same channel/thread.
        client: Slack WebClient scoped to this workspace.
    """
    ack()  # Acknowledge immediately so Slack doesn't time out

    channel_id = command['channel_id']
    team_id = command['team_id']
    thread_ts = command.get('thread_ts')
    text = command.get('text', '').strip()

    # Check if user specified a different channel: /summarize #channel-name
    target_channel_id = channel_id
    target_channel_name = None

    channel_mention = re.search(r'#?([\w-]+)', text)
    if channel_mention and text:
        target_channel_name = channel_mention.group(1)
        bot_token = get_installation_token(team_id)
        found_id = get_channel_id(target_channel_name, bot_token)
        if found_id:
            target_channel_id = found_id
        else:
            say(f"❌ Couldn't find channel *#{target_channel_name}*.")
            return

    bot_token = get_installation_token(team_id)
    if not bot_token:
        say("❌ Bot not properly installed. Please reinstall via /slack/install.")
        return

    if thread_ts and not target_channel_name:
        # Summarize the current thread
        say("🔍 Summarizing this thread...", thread_ts=thread_ts)
        summary = summarize_thread(channel_id, thread_ts, bot_token)
        say(f"📋 *Thread Summary:*\n\n{summary}", thread_ts=thread_ts)
    else:
        # Summarize the channel
        label = f"#{target_channel_name}" if target_channel_name else "this channel"
        say(f"🔍 Summarizing *{label}* (last 24 hours)...")
        summary = summarize_channel_history(target_channel_id, bot_token)
        say(f"📋 *Summary of {label}:*\n\n{summary}")


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

@flask_app.route("/slack/install", methods=["GET"])
def install():
    """
    Entry point for the Slack OAuth installation flow.

    Generates a cryptographically random state token (stored in SQLite to
    prevent CSRF attacks), then redirects the user to Slack's OAuth consent
    page with the required scopes and our redirect URI.

    Returns:
        Response: A redirect to Slack's OAuth authorization page.
    """
    state = secrets.token_urlsafe(32)
    store_oauth_state(state)

    scopes = ",".join([
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "commands",
        "groups:history",
        "groups:read",
        "im:history",
        "im:write",
        "users:read",
    ])

    auth_url = (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={os.environ.get('SLACK_CLIENT_ID')}"
        f"&scope={scopes}"
        f"&redirect_uri={os.environ.get('SLACK_REDIRECT_URI')}"
        f"&state={state}"
    )

    return flask_redirect(auth_url)


@flask_app.route("/slack/oauth_redirect", methods=["GET"])
def oauth_redirect():
    """
    Callback URL that Slack redirects to after the user approves the installation.

    Manually exchanges the one-time authorization code for a permanent bot token
    by calling Slack's oauth.v2.access API directly via HTTP POST. Stores the
    token in SQLite and returns a success page to the user.

    This URL must be registered in your Slack app under:
    OAuth & Permissions → Redirect URLs

    Returns:
        Response: An HTML success or error page.
    """
    error = request.args.get('error')
    if error:
        print(f"OAuth error from Slack: {error}")
        return f"<h1>Installation cancelled</h1><p>Reason: {error}</p>", 400

    code = request.args.get('code')
    state = request.args.get('state')

    if not code:
        return "<h1>Error</h1><p>No authorization code received from Slack.</p>", 400

    # Verify the state token to prevent CSRF attacks
    if not state or not verify_and_consume_state(state):
        print(f"Invalid or expired state token: {state}")
        return "<h1>Error</h1><p>Invalid state token. Please try installing again.</p>", 400

    # Exchange the authorization code for a bot token directly via HTTP POST.
    # This bypasses Bolt's internal OAuth handler to avoid reverse-proxy URL issues.
    response = http_requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "code": code,
            "client_id": os.environ.get("SLACK_CLIENT_ID"),
            "client_secret": os.environ.get("SLACK_CLIENT_SECRET"),
            "redirect_uri": os.environ.get("SLACK_REDIRECT_URI"),
        },
        timeout=10
    )

    data = response.json()
    print(f"OAuth exchange response: ok={data.get('ok')}, error={data.get('error')}")

    if not data.get("ok"):
        return (
            f"<h1>Installation failed</h1>"
            f"<p>Slack returned an error: <strong>{data.get('error')}</strong></p>"
            f"<p>Please <a href='/slack/install'>try again</a>.</p>"
        ), 400

    # Store the installation details in our database
    team_id = data["team"]["id"]
    team_name = data["team"]["name"]
    bot_token = data["access_token"]
    bot_user_id = data.get("bot_user_id", "")

    store_installation(team_id, team_name, bot_token, bot_user_id)
    print(f"Successfully installed in workspace: {team_name} ({team_id})")

    return """
    <html>
    <body style="font-family: sans-serif; text-align: center; padding: 60px;">
        <h1>✅ Standup Agent installed!</h1>
        <p>The bot has been successfully added to <strong>{}</strong>.</p>
        <p>Send it a DM in Slack to get started.</p>
    </body>
    </html>
    """.format(team_name)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    """
    Webhook endpoint that receives all incoming Slack events.

    Slack sends an HTTP POST to this URL every time an event occurs
    (messages, mentions, reactions, etc.). The SlackRequestHandler
    verifies the request signature and dispatches it to the correct
    Slack Bolt event handler above.

    Slack retries failed events up to 3 times with the X-Slack-Retry-Num
    header. We immediately acknowledge retries with 200 to prevent the bot
    from processing the same message multiple times after coming back online.

    Returns:
        Response: HTTP 200 with Slack's expected response payload.
    """
    # Layer 1: Ignore Slack's automatic retries.
    # When our response takes longer than 3 seconds (e.g. Anthropic API call),
    # Slack resends the event with X-Slack-Retry-Num header. We return 200
    # immediately so Slack stops retrying.
    if request.headers.get("X-Slack-Retry-Num"):
        print(f"Ignoring Slack retry #{request.headers.get('X-Slack-Retry-Num')}")
        return jsonify({"status": "ok"}), 200

    # Layer 2: Deduplicate by event_id as a safety net.
    # Each unique Slack event has a stable event_id across all delivery attempts.
    # If we've already processed this event_id, silently return 200.
    body = request.get_data(as_text=True)
    try:
        payload = json.loads(body)
        event_id = payload.get("event_id")
        if event_id:
            if event_id in processed_event_ids:
                print(f"Ignoring duplicate event: {event_id}")
                return jsonify({"status": "ok"}), 200
            processed_event_ids.add(event_id)
            if len(processed_event_ids) > 1000:
                processed_event_ids.clear()
    except Exception:
        pass

    return handler.handle(request)


@flask_app.route("/auth/google", methods=["GET"])
def google_auth():
    """
    Start the Google Calendar OAuth flow for a specific user.

    Requires both ?team_id= and ?user_id= query parameters so the returned
    token is saved against the correct individual user within a workspace.
    The bot sends each user their unique personal link automatically on their
    first DM so every team member can connect their own calendar.

    Example: https://<railway-url>/auth/google?team_id=T01ERGZJCPQ&user_id=U01ABCDEF
    """
    team_id = request.args.get("team_id")
    user_id = request.args.get("user_id")
    if not team_id or not user_id:
        return (
            "<h1>Error</h1>"
            "<p>Missing team_id or user_id. Please use the link sent by the bot in Slack.</p>"
        ), 400

    creds_path = load_google_credentials_file()
    if not creds_path:
        return "<h1>Error</h1><p>GOOGLE_CREDENTIALS_JSON environment variable not set in Railway.</p>", 500

    # Derive the base URL from SLACK_REDIRECT_URI which is already confirmed
    # working in Railway — avoids needing a separate GOOGLE_REDIRECT_URI variable.
    # e.g. https://worker-production-bb20.up.railway.app/slack/oauth_redirect
    #   -> https://worker-production-bb20.up.railway.app/auth/google/callback
    slack_redirect = os.environ.get("SLACK_REDIRECT_URI", "")
    base_url = slack_redirect.rsplit("/slack/oauth_redirect", 1)[0]
    redirect_uri = f"{base_url}/auth/google/callback"
    flow = Flow.from_client_secrets_file(
        creds_path,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent'
    )

    # Map the OAuth state to both team_id and user_id so the callback knows
    # exactly which user to save the token for
    os.makedirs("data", exist_ok=True)
    with open(f"data/google_state_{state}", "w") as f:
        f.write(f"{team_id}:{user_id}")

    return flask_redirect(auth_url)


@flask_app.route("/auth/google/callback", methods=["GET"])
def google_auth_callback():
    """
    Google OAuth callback — exchanges the code for a token and saves it
    per-user in the database.

    Looks up the team_id and user_id from the state file written during
    /auth/google, then stores the credentials in the google_tokens table so
    each individual user has their own independent Google Calendar connection.
    """
    error = request.args.get("error")
    if error:
        return f"<h1>Authorization failed</h1><p>{error}</p>", 400

    state = request.args.get("state")

    # Recover the team_id:user_id pair associated with this OAuth state
    state_file = f"data/google_state_{state}"
    try:
        with open(state_file, "r") as f:
            state_data = f.read().strip()
        os.remove(state_file)
        # State file now stores "team_id:user_id"
        if ":" in state_data:
            team_id, user_id = state_data.split(":", 1)
        else:
            # Legacy format (just team_id) — fall back gracefully
            team_id = state_data
            user_id = get_workspace_owner(team_id) or ""
    except FileNotFoundError:
        return (
            "<h1>Error</h1>"
            "<p>Session expired. Please use the /auth/google link from the bot again.</p>"
        ), 400

    creds_path = load_google_credentials_file()
    slack_redirect = os.environ.get("SLACK_REDIRECT_URI", "")
    base_url = slack_redirect.rsplit("/slack/oauth_redirect", 1)[0]
    redirect_uri = f"{base_url}/auth/google/callback"

    flow = Flow.from_client_secrets_file(
        creds_path,
        scopes=SCOPES,
        state=state,
        redirect_uri=redirect_uri
    )

    # Railway reverse proxy strips HTTPS — restore it for token exchange
    auth_response = request.url.replace("http://", "https://")
    flow.fetch_token(authorization_response=auth_response)

    # Save the token against this specific user (not just the workspace)
    store_google_token(team_id, user_id, flow.credentials)
    print(f"Google Calendar connected for user {user_id} in workspace {team_id}")

    return """
    <html>
    <body style="font-family: sans-serif; text-align: center; padding: 60px;">
        <h1>✅ Google Calendar connected!</h1>
        <p>Your personal calendar is now linked to your Standup Agent.</p>
        <p>You can close this tab and return to Slack.</p>
    </body>
    </html>
    """


@flask_app.route("/health", methods=["GET"])
def health_check():
    """
    Simple health check endpoint for Railway and uptime monitors.

    Returns:
        JSON: {"status": "ok"} with HTTP 200 to confirm the server is running.
    """
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def startup() -> None:
    """
    Initialise the database and start the background scheduler.

    This is called at module load time so it runs whether the app is started
    via Gunicorn (production) or python3 directly (local development).
    Gunicorn does not execute the __main__ block, so startup logic must live
    here at module level.
    """
    init_db()

    print("Bot is running in HTTP mode!")
    print("Install URL: /slack/install")
    print("OAuth Redirect URL: /slack/oauth_redirect")
    print("Events endpoint: /slack/events")
    print("Daily standup scheduled for 9:00 AM")
    print("Auto-response monitoring enabled - will respond if you don't reply in 5 min")
    print("Calendar features: read, create, delete events with attendees")

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()


# Run startup immediately when the module is imported (works with both
# Gunicorn and direct python3 execution)
startup()


if __name__ == "__main__":
    # Local development only — Gunicorn handles this in production
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)
