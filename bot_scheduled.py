"""
Standup Agent - Slack Bot with Google Calendar Integration
==========================================================
A Slack bot that acts as a personal AI assistant. It:
  - Sends a daily standup prompt every morning at 9:00 AM with your calendar for the day
  - Responds to direct messages with AI-powered answers and calendar context
  - Monitors channel mentions and auto-responds on your behalf if you haven't replied within 5 minutes
  - Responds directly when @mentioned in any channel it has been added to
  - Supports creating, reading, and deleting Google Calendar events via natural language in DMs

Setup:
  1. Copy .env.example to .env and fill in your credentials
  2. Place your Google OAuth credentials in credentials.json
  3. Run: python3 bot_scheduled.py
  4. On first run, a browser window will open for Google Calendar authorization

Dependencies:
  slack-bolt, anthropic, google-auth, google-api-python-client, schedule, python-dotenv

Author: Kingsley Mkpandiok
"""

import os
import datetime
import pickle
import schedule
import time
import threading
import json

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
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

# Slack Bolt app — authenticates using the bot token from .env
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# Anthropic client — used for all AI-generated responses
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ---------------------------------------------------------------------------
# Constants & Global State
# ---------------------------------------------------------------------------

# Google Calendar OAuth scopes — full access needed to create and delete events
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Slack user ID of the bot owner, auto-captured from the first DM or channel message.
# Used to detect when the owner has responded to a mention so the auto-reply is cancelled.
YOUR_USER_ID: str | None = None
YOUR_SLACK_USER_ID: str | None = None

# Dictionary tracking channel mentions that haven't received a reply yet.
# Key format: "{channel_id}:{thread_ts}"
# Value: dict with channel, thread_ts, original message text, and timestamp
pending_mentions: dict = {}

# Messages containing any of these keywords will be skipped by the auto-responder
# to avoid the bot inadvertently weighing in on sensitive conversations.
SENSITIVE_KEYWORDS = ['personal', 'private', 'confidential', 'sensitive', '1:1', 'one-on-one']


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

def check_user_active() -> bool:
    """
    Check whether the bot owner is currently marked as 'active' on Slack.

    This is used by the auto-responder to skip sending a reply if the owner
    appears to be online (they may just be slow to respond).

    Returns:
        bool: True if the owner's Slack presence is 'active', False otherwise
              or if the presence check fails.
    """
    try:
        if YOUR_SLACK_USER_ID:
            response = app.client.users_getPresence(user=YOUR_SLACK_USER_ID)
            return response['presence'] == 'active'
    except Exception as e:
        print(f"Error checking presence: {e}")
    return False


def search_slack_history(query: str, channel_id: str) -> list:
    """
    Search Slack message history for messages relevant to a given query.

    Results are used to give the AI assistant context about past conversations
    before generating an auto-reply.

    Args:
        query (str): The search string (typically the first 100 chars of the mention).
        channel_id (str): The Slack channel ID (currently unused by the API call
                          but kept for future scoped search support).

    Returns:
        list: Up to 3 matching Slack message objects, or an empty list on failure.
    """
    try:
        result = app.client.search_messages(query=query, count=5)
        if result['messages']['matches']:
            return result['messages']['matches'][:3]
    except Exception as e:
        print(f"Error searching history: {e}")
    return []


# ---------------------------------------------------------------------------
# Auto-Reply Logic
# ---------------------------------------------------------------------------

def auto_respond_to_mention(channel: str, thread_ts: str, original_message: str) -> None:
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
        channel (str): Slack channel ID where the mention occurred.
        thread_ts (str): Timestamp of the thread root message (used as thread identifier).
        original_message (str): Full text of the message that mentioned the owner.
    """
    global YOUR_SLACK_USER_ID

    # Wait the full 5-minute window before doing anything
    time.sleep(300)

    key = f"{channel}:{thread_ts}"

    # Bail out if the owner already responded (key removed from pending_mentions)
    if key not in pending_mentions:
        return

    # Bail out if the owner is now showing as active on Slack
    if check_user_active():
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
        search_results = search_slack_history(original_message[:100], channel)

        context = f"Someone asked: '{original_message}'\n"
        if search_results:
            context += "\nRelevant past messages:\n"
            for msg in search_results:
                context += f"- {msg.get('text', '')[:200]}\n"

        ai_prompt = f"""{context}

You are Kingsley's AI assistant. Kingsley hasn't responded yet. Provide a helpful response that:
1. Acknowledges you're the assistant
2. Provides useful information if possible
3. Asks clarifying questions if needed
4. Mentions Kingsley will follow up

Keep it brief and professional."""

        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": ai_prompt}]
        )

        reply = (
            f"👋 Hi! I'm Kingsley's AI assistant. He hasn't responded yet, but let me help:\n\n"
            f"{response.content[0].text}\n\n"
            f"_Kingsley will follow up when he's available._"
        )

        # Post the AI reply in the same thread as the original mention
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=reply
        )

        print(f"Auto-responded to mention in {channel}")

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
    Send the daily standup prompt to the bot owner as a Slack DM.

    Fetches today's Google Calendar events and includes them in the message
    so the owner has full context when planning their day.

    This function is registered with the `schedule` library and fires every
    day at 09:00 (in whatever timezone the host machine is set to).
    """
    global YOUR_USER_ID
    if YOUR_USER_ID:
        try:
            calendar_info = get_events_for_date(0)
            message = f"Good morning! What are you working on today?\n\nYour calendar:\n{calendar_info}"
            app.client.chat_postMessage(channel=YOUR_USER_ID, text=message)
            print(f"Daily standup sent at {datetime.datetime.now()}")
        except Exception as e:
            print(f"Error sending standup: {e}")


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

    Also captures the owner's Slack user ID on the first DM if it hasn't been
    set yet.

    Args:
        event (dict): The Slack event payload for the incoming message.
        say (callable): Slack Bolt's `say` function to reply in the same channel/DM.
    """
    global YOUR_USER_ID, YOUR_SLACK_USER_ID

    user = event.get('user')
    user_message = event.get('text', '')
    user_message_lower = user_message.lower()

    # Capture the owner's Slack user ID the first time they send a DM
    if not YOUR_USER_ID:
        YOUR_USER_ID = user
        YOUR_SLACK_USER_ID = user
        print(f"User ID saved from DM: {YOUR_USER_ID}")

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
    global YOUR_SLACK_USER_ID, YOUR_USER_ID

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
    channel = event.get('channel')
    text = event.get('text', '')
    user = event.get('user')
    ts = event['ts']
    # Use thread_ts if this is a reply; otherwise the message itself is the thread root
    thread_ts = event.get('thread_ts', ts)

    # Auto-capture the owner's user ID from the first channel message seen
    if not YOUR_SLACK_USER_ID and not YOUR_USER_ID:
        YOUR_SLACK_USER_ID = user
        YOUR_USER_ID = user
        print(f"User ID saved from channel message: {user}")
        return

    # If someone else mentioned the owner, start the auto-reply countdown
    if YOUR_SLACK_USER_ID and f"<@{YOUR_SLACK_USER_ID}>" in text:
        if user != YOUR_SLACK_USER_ID:
            key = f"{channel}:{thread_ts}"

            # Register this mention as pending
            pending_mentions[key] = {
                'channel': channel,
                'thread_ts': thread_ts,
                'message': text,
                'timestamp': time.time()
            }

            # Spin up a daemon thread — it will wait 5 min then auto-reply if needed
            threading.Thread(
                target=auto_respond_to_mention,
                args=(channel, thread_ts, text),
                daemon=True
            ).start()

            print(f"Tracking mention in {channel}, will auto-respond in 5 min if no reply")

    # If the owner replied in a thread with a pending mention, cancel the auto-reply
    if YOUR_SLACK_USER_ID and user == YOUR_SLACK_USER_ID:
        key = f"{channel}:{thread_ts}"
        if key in pending_mentions:
            print(f"Kingsley responded, canceling auto-response for {key}")
            del pending_mentions[key]


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Bot is running!")
    print("Daily standup scheduled for 9:00 AM")
    print("Auto-response monitoring enabled - will respond if you don't reply in 5 min")
    print("Calendar features: read, create, delete events with attendees")

    # Start the schedule loop in a background daemon thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Start the Slack Socket Mode connection — this blocks until the bot is stopped
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
