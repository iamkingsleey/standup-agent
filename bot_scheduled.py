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
import sqlite3
import secrets
import datetime
import pickle
import schedule
import time
import threading
import json

import requests as http_requests
from flask import Flask, request, jsonify, redirect as flask_redirect
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient
from anthropic import Anthropic
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
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
# Google Calendar Helpers
# ---------------------------------------------------------------------------

def get_calendar_service():
    """
    Authenticate with Google Calendar and return an authorized service client.

    On first run (or after deleting token.pickle), this opens a browser window
    for OAuth authorization. On subsequent runs it loads the saved token and
    refreshes it automatically if expired.

    Returns:
        googleapiclient.discovery.Resource: Authorized Google Calendar API service.

    Raises:
        FileNotFoundError: If credentials.json is missing.
        google.auth.exceptions.RefreshError: If the refresh token has been revoked.
    """
    creds = None

    # Load existing credentials from disk if available
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    # If there are no valid credentials, refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Silently refresh the access token using the refresh token
            creds.refresh(Request())
        else:
            # First-time auth: open browser for user consent
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

        # Persist the new/refreshed credentials for future runs
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('calendar', 'v3', credentials=creds)


def get_events_for_date(days_offset: int = 0) -> str:
    """
    Fetch all Google Calendar events for a given day and return them as a
    formatted string.

    Args:
        days_offset (int): Number of days from today.
                           0 = today, 1 = tomorrow, 2 = day after, etc.

    Returns:
        str: A newline-separated list of events (e.g. "- Team Standup at 09:00"),
             or a friendly message if no events are found, or an error string
             if the API call fails.
    """
    try:
        service = get_calendar_service()
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
    summary: str,
    start_time: datetime.datetime,
    duration_minutes: int = 60,
    attendee_emails: list[str] | None = None
) -> str:
    """
    Create a new Google Calendar event and optionally invite attendees.

    Args:
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
        service = get_calendar_service()
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


def delete_calendar_event(event_title: str, days_offset: int = 0) -> str:
    """
    Find and delete a calendar event by partial title match on a given day.

    The search is case-insensitive and matches any event whose summary contains
    the provided title string. If multiple matches are found, the user is asked
    to be more specific rather than deleting all of them.

    Args:
        event_title (str): Partial or full title of the event to delete.
        days_offset (int): 0 = today, 1 = tomorrow, etc. Defaults to 0.

    Returns:
        str: Confirmation of deletion, a prompt to be more specific if multiple
             events match, a not-found message, or an error string on failure.
    """
    try:
        service = get_calendar_service()
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

    # Bail out if the owner is now showing as active on Slack
    if check_user_active(owner_user_id, bot_token):
        print(f"User is active, skipping auto-response for {key}")
        del pending_mentions[key]
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
            calendar_info = get_events_for_date(0)
            message = (
                f"Good morning! What are you working on today?\n\n"
                f"Your calendar:\n{calendar_info}"
            )
            client.chat_postMessage(channel=user_id, text=message)
            print(f"Daily standup sent to {user_id} in workspace {team_id}")

        except Exception as e:
            print(f"Error sending standup to workspace {team_id}: {e}")


# Register the standup job — fires every day at 09:00
schedule.every().day.at("09:00").do(send_daily_standup)


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

    # Register the sender as workspace owner on their first DM to the bot
    if team_id and not get_workspace_owner(team_id):
        set_workspace_owner(team_id, user)
        print(f"Workspace owner set: user {user} in team {team_id}")

    print(f"Processing DM: {user_message[:50]}...")

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
                result = delete_calendar_event(delete_details['event_title'], days)
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
        calendar_info = f"\n\nCalendar information:\n{get_events_for_date(days_offset)}"

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": user_message + calendar_info}]
    )
    say(response.content[0].text)


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
    # Ignore messages sent by bots (including this bot itself)
    if event.get('subtype') == 'bot_message':
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
    # Ignore Slack's automatic retries to prevent duplicate processing.
    # When our response takes longer than 3 seconds (e.g. Anthropic API call),
    # Slack resends the event with X-Slack-Retry-Num header. We return 200
    # immediately so Slack stops retrying — the original is still processing.
    if request.headers.get("X-Slack-Retry-Num"):
        print(f"Ignoring Slack retry #{request.headers.get('X-Slack-Retry-Num')}")
        return jsonify({"status": "ok"}), 200

    return handler.handle(request)


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
