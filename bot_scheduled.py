import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from anthropic import Anthropic
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import datetime
import pickle
import schedule
import time
import threading
import re
import json
from collections import defaultdict

load_dotenv()

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar']
YOUR_USER_ID = None
YOUR_SLACK_USER_ID = None

# Track messages that mention you and haven't been responded to
pending_mentions = {}
# Keywords to skip auto-response for sensitive topics
SENSITIVE_KEYWORDS = ['personal', 'private', 'confidential', 'sensitive', '1:1', 'one-on-one']

def get_calendar_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return build('calendar', 'v3', credentials=creds)

def get_events_for_date(days_offset=0):
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
        
        if not events:
            day_name = "today" if days_offset == 0 else "tomorrow" if days_offset == 1 else f"in {days_offset} days"
            return f"No events scheduled for {day_name}."
        
        event_list = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            event_list.append(f"- {event['summary']} at {start}")
        
        return "\n".join(event_list)
    except Exception as e:
        return f"Could not fetch calendar: {str(e)}"

def create_calendar_event(summary, start_time, duration_minutes=60, attendee_emails=None):
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
        
        if attendee_emails:
            event['attendees'] = [{'email': email} for email in attendee_emails]
            event['sendUpdates'] = 'all'
        
        event = service.events().insert(calendarId='primary', body=event, sendUpdates='all').execute()
        
        response_text = f"Event created: {event.get('summary')} at {event['start'].get('dateTime')}\nLink: {event.get('htmlLink')}"
        
        if attendee_emails:
            response_text += f"\n\nInvitations sent to: {', '.join(attendee_emails)}"
        
        return response_text
    except Exception as e:
        return f"Could not create event: {str(e)}"

def delete_calendar_event(event_title, days_offset=0):
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
        
        matching_events = [e for e in events if event_title.lower() in e['summary'].lower()]
        
        if not matching_events:
            return f"No events found matching '{event_title}'"
        
        if len(matching_events) > 1:
            event_list = "\n".join([f"- {e['summary']} at {e['start'].get('dateTime', e['start'].get('date'))}" for e in matching_events])
            return f"Found multiple events matching '{event_title}':\n{event_list}\n\nPlease be more specific."
        
        event_to_delete = matching_events[0]
        service.events().delete(calendarId='primary', eventId=event_to_delete['id'], sendUpdates='all').execute()
        
        return f"Deleted event: {event_to_delete['summary']} at {event_to_delete['start'].get('dateTime', event_to_delete['start'].get('date'))}\n\nCancellation emails sent to attendees."
        
    except Exception as e:
        return f"Could not delete event: {str(e)}"

def check_user_active():
    """Check if Kingsley is currently active on Slack"""
    try:
        if YOUR_SLACK_USER_ID:
            response = app.client.users_getPresence(user=YOUR_SLACK_USER_ID)
            return response['presence'] == 'active'
    except Exception as e:
        print(f"Error checking presence: {e}")
    return False

def search_slack_history(query, channel_id):
    """Search Slack message history for relevant information"""
    try:
        result = app.client.search_messages(query=query, count=5)
        if result['messages']['matches']:
            return result['messages']['matches'][:3]
    except Exception as e:
        print(f"Error searching history: {e}")
    return []

def auto_respond_to_mention(channel, thread_ts, original_message):
    """Auto-respond when Kingsley doesn't reply within 5 minutes"""
    global YOUR_SLACK_USER_ID
    
    # Wait 5 minutes
    time.sleep(300)
    
    # Check if still pending (Kingsley hasn't responded)
    key = f"{channel}:{thread_ts}"
    if key not in pending_mentions:
        return
    
    # Check if user is now active
    if check_user_active():
        print(f"User is active, skipping auto-response for {key}")
        del pending_mentions[key]
        return
    
    # Check for sensitive keywords
    message_lower = original_message.lower()
    if any(keyword in message_lower for keyword in SENSITIVE_KEYWORDS):
        print(f"Sensitive content detected, skipping auto-response for {key}")
        del pending_mentions[key]
        return
    
    try:
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
        
        reply = f"👋 Hi! I'm Kingsley's AI assistant. He hasn't responded yet, but let me help:\n\n{response.content[0].text}\n\n_Kingsley will follow up when he's available._"
        
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=reply
        )
        
        print(f"Auto-responded to mention in {channel}")
        
    except Exception as e:
        print(f"Error in auto-response: {e}")
    
    if key in pending_mentions:
        del pending_mentions[key]

def send_daily_standup():
    global YOUR_USER_ID
    if YOUR_USER_ID:
        try:
            calendar_info = get_events_for_date(0)
            message = f"Good morning! What are you working on today?\n\nYour calendar:\n{calendar_info}"
            app.client.chat_postMessage(channel=YOUR_USER_ID, text=message)
            print(f"Daily standup sent at {datetime.datetime.now()}")
        except Exception as e:
            print(f"Error sending standup: {e}")

schedule.every().day.at("09:00").do(send_daily_standup)

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)


# ============================================================
# HANDLE DM MESSAGES
# ============================================================
def process_direct_message(event, say):
    """Process a direct message from the user"""
    global YOUR_USER_ID, YOUR_SLACK_USER_ID

    user = event.get('user')
    user_message = event.get('text', '')
    user_message_lower = user_message.lower()

    # Save user ID on first DM
    if not YOUR_USER_ID:
        YOUR_USER_ID = user
        YOUR_SLACK_USER_ID = user
        print(f"User ID saved from DM: {YOUR_USER_ID}")

    print(f"Processing DM: {user_message[:50]}...")

    calendar_info = ""
    days_offset = None

    # Check for delete requests
    if ("delete" in user_message_lower or "cancel" in user_message_lower or "remove" in user_message_lower) and ("meeting" in user_message_lower or "event" in user_message_lower or "call" in user_message_lower):
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

    # Check for scheduling requests
    if "schedule" in user_message_lower and ("meeting" in user_message_lower or "call" in user_message_lower or "event" in user_message_lower):
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

                result = create_calendar_event(event_details['title'], event_datetime, duration, attendees if attendees else None)
                say(result)
                return
            else:
                say("I couldn't extract all the event details.")
                return
        except Exception as e:
            say(f"I had trouble understanding the scheduling request. Error: {str(e)}")
            return

    # Regular calendar queries
    if "tomorrow" in user_message_lower:
        days_offset = 1
    elif "today" in user_message_lower or "calendar" in user_message_lower or "meeting" in user_message_lower:
        days_offset = 0

    if days_offset is not None:
        calendar_info = f"\n\nCalendar information:\n{get_events_for_date(days_offset)}"

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": user_message + calendar_info}]
    )
    say(response.content[0].text)


# ============================================================
# MAIN EVENT HANDLER - Routes DMs and channel messages
# ============================================================
@app.event("message")
def handle_message_event(event, say):
    global YOUR_SLACK_USER_ID, YOUR_USER_ID
    
    # Skip bot messages
    if event.get('subtype') == 'bot_message':
        return
    
    # Skip message_changed, message_deleted, etc.
    if event.get('subtype') is not None:
        return

    # ---- DM: route to direct message handler ----
    if event.get('channel_type') == 'im':
        process_direct_message(event, say)
        return

    # ---- CHANNEL MESSAGE: monitor for mentions ----
    channel = event.get('channel')
    text = event.get('text', '')
    user = event.get('user')
    ts = event['ts']
    thread_ts = event.get('thread_ts', ts)
    
    # Save user ID on first message
    if not YOUR_SLACK_USER_ID and not YOUR_USER_ID:
        YOUR_SLACK_USER_ID = user
        YOUR_USER_ID = user
        print(f"User ID saved from channel message: {user}")
        return
    
    # Check if this message mentions Kingsley
    if YOUR_SLACK_USER_ID and f"<@{YOUR_SLACK_USER_ID}>" in text:
        if user != YOUR_SLACK_USER_ID:
            key = f"{channel}:{thread_ts}"
            
            pending_mentions[key] = {
                'channel': channel,
                'thread_ts': thread_ts,
                'message': text,
                'timestamp': time.time()
            }
            
            threading.Thread(
                target=auto_respond_to_mention,
                args=(channel, thread_ts, text),
                daemon=True
            ).start()
            
            print(f"Tracking mention in {channel}, will auto-respond in 5 min if no reply")
    
    # Check if Kingsley replied to a pending mention
    if YOUR_SLACK_USER_ID and user == YOUR_SLACK_USER_ID:
        key = f"{channel}:{thread_ts}"
        if key in pending_mentions:
            print(f"Kingsley responded, canceling auto-response for {key}")
            del pending_mentions[key]


if __name__ == "__main__":
    print("Bot is running!")
    print("Daily standup scheduled for 9:00 AM")
    print("Auto-response monitoring enabled - will respond if you don't reply in 5 min")
    print("Calendar features: read, create, delete events with attendees")
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
