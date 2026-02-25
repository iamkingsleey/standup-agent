
qqimport os
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
import re

load_dotenv()

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

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

@app.message("")
def handle_message(message, say):
    user_message = message['text'].lower()
    
    calendar_info = ""
    days_offset = None
    
    if "tomorrow" in user_message:
        days_offset = 1
    elif "today" in user_message or "calendar" in user_message or "meeting" in user_message or "schedule" in user_message:
        days_offset = 0
    
    if days_offset is not None:
        calendar_info = f"\n\nCalendar information:\n{get_events_for_date(days_offset)}"
    
    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": message['text'] + calendar_info}]
    )
    say(response.content[0].text)

if __name__ == "__main__":
    print("Bot is running!")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
