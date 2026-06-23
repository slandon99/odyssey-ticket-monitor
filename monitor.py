import os
import requests
from datetime import datetime

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13/showtimes/all"

seen = set()

def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg}
    )

def allowed(dt):
    if dt.month == 7 and dt.day == 16:
        return dt.hour >= 18
    if 17 <= dt.day <= 19 and dt.month == 7:
        return True
    if 20 <= dt.day <= 24 and dt.month == 7:
        return dt.hour >= 19
    if 25 <= dt.day <= 26 and dt.month == 7:
        return True
    if 27 <= dt.day <= 28 and dt.month == 7:
        return dt.hour >= 19
    if dt.month == 8 and dt.day in [8, 9]:
        return True
    return False

def check():
    r = requests.get(URL, timeout=10)
    text = r.text.lower()
    now = datetime.now()

    if "imax" in text and "odyssey" in text:
        if allowed(now):
            key = f"{now.date()}-{now.hour}"
            if key not in seen:
                seen.add(key)
                send(
                    "🎬 THE ODYSSEY ALERT\n\n"
                    "AMC Lincoln Square IMAX\n"
                    f"{now.strftime('%b %d %I:%M %p')}\n\n"
                    "Tickets may be available:\n" + URL
                )

if __name__ == "__main__":
    check()
