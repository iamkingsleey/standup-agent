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

load_dotenv()

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar']
YOUR_USER_ID = None

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

@app.message("")
def handle_message(message, say):
    global YOUR_USER_ID
    if not YOUR_USER_ID:
        YOUR_USER_ID = message['user']
        print(f"User ID saved: {YOUR_USER_ID}")
    
    user_message = message['text']
    user_message_lower = user_message.lower()
    
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
            import json
            extracted_text = deletion_response.content[0].text.strip()
            extracted_text = extracted_text.replace('```json', '').replace('```', '').strip()
            delete_details = json.loads(extracted_text)
            
            if delete_details.get('event_title'):
                days = 1 if delete_details.get('date_context') == 'tomorrow' else 0
                result = delete_calendar_event(delete_details['event_title'], days)
                say(result)
                return
            else:
                say("Please specify which event you want to delete. Example: 'Delete the Team Sync meeting tomorrow'")
                return
        except Exception as e:
            say(f"I had trouble understanding the deletion request. Error: {str(e)}")
            return
    
    # Check for scheduling requests
    if "schedule" in user_message_lower and ("meeting" in user_message_lower or "call" in user_message_lower or "event" in user_message_lower):
        extraction_prompt = f"""Extract the event details from this message: "{user_message}"

Return ONLY a JSON object with these fields:
- title: the event name/summary
- date: YYYY-MM-DD format (if 'tomorrow' use tomorrow's date, if 'today' use today's date)
- time: HH:MM format in 24-hour time
- duration: number of minutes (default 60 if not specified)
- attendees: array of email addresses (empty array if none mentioned)

If any information is missing, set it to null or empty array for attendees.
Today's date is {datetime.datetime.now().strftime('%Y-%m-%d')}"""
        
        extraction_response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": extraction_prompt}]
        )
        
        try:
            import json
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
                say("I couldn't extract all the event details. Please specify the event title, date, and time.")
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

if __name__ == "__main__":
    print("Bot is running!")
    print("Daily standup scheduled for 9:00 AM")
    print("Calendar features: read, create, delete events with attendees!")
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
