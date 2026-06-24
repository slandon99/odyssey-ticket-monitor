"""
Odyssey IMAX ticket monitor for AMC Lincoln Square 13.

What this does, in plain terms:
1. Opens Fandango's page for this theater in a real browser (Playwright),
   once per date you care about, using a date parameter directly in the
   URL. This replaced an earlier version that tried AMC's own site,
   which got stuck loading and never showed real showtime data for an
   automated browser.
2. On each date's page, scans the rendered text for "Odyssey" and checks
   whether an IMAX showtime is listed near it.
3. Compares any IMAX showtime found against your time window rule for
   that date.
4. Sends one Telegram alert per matching showtime, only once ever,
   using seen_showtimes.json to remember what has already been sent.
5. Always saves a screenshot from the first date checked and the full
   rendered page text for every date, as debug files, so if the parsing
   logic is wrong, we can see exactly what the real page looked like and
   fix it quickly.

Known limitation: the part of this script that finds "Odyssey" and
"IMAX" text on the page (see find_matches below) was written without
being able to load the real Fandango page directly. It is a reasonable
best attempt, not something already confirmed against the live site.
Check the debug_page_text.txt artifact after the first real run. If
nothing matches when it should, that file will show why.
"""

import json
import os
import re
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

THEATRE_URL_TEMPLATE = "https://www.fandango.com/amc-lincoln-square-13-aabqi/theater-page?date={date}&a=11533"

MOVIE_KEYWORD = "odyssey"
FORMAT_KEYWORDS = ["imax", "70mm", "dolby cinema", "standard", "real d 3d", "prime", "dine-in"]

SEEN_FILE = "seen_showtimes.json"

# Date in YYYY-MM-DD format, mapped to the earliest allowed hour (24 hour
# clock) for that date, or None meaning any time of day is fine.
TIME_WINDOWS = {
    "2026-07-16": 18,
    "2026-07-17": None,
    "2026-07-18": None,
    "2026-07-19": None,
    "2026-07-20": 19,
    "2026-07-21": 19,
    "2026-07-22": 19,
    "2026-07-23": 19,
    "2026-07-24": 19,
    "2026-07-25": None,
    "2026-07-26": None,
    "2026-07-27": 19,
    "2026-07-28": 19,
    "2026-08-08": None,
    "2026-08-09": None,
}


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
    response.raise_for_status()


def parse_hour(time_text):
    """Turn '7:40p', '7:40pm', or '7:40 PM' into an hour on a 24 hour clock."""
    match = re.search(r"(\d{1,2}):(\d{2})\s*([ap])m?", time_text.lower())
    if not match:
        return None
    hour, _minute, meridian = match.groups()
    hour = int(hour)
    if meridian == "p" and hour != 12:
        hour += 12
    if meridian == "a" and hour == 12:
        hour = 0
    return hour


def find_matches(page_text):
    """
    Scan the rendered page text for IMAX showtimes of The Odyssey.
    Returns a list of time strings found under an IMAX heading near
    an Odyssey mention.
    """
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    matches = []

    for i, line in enumerate(lines):
        if MOVIE_KEYWORD not in line.lower():
            continue

        current_format = None
        # Look at the next 45 lines after the Odyssey title for format
        # labels and showtime stamps, enough to cover every format
        # section (IMAX, plain 70mm, Dolby Cinema, Standard) for one day.
        for nearby_line in lines[i + 1: i + 46]:
            lower = nearby_line.lower()

            matched_format = None
            for fmt in FORMAT_KEYWORDS:
                if fmt in lower:
                    matched_format = fmt
                    break
            if matched_format:
                current_format = matched_format
                continue

            if current_format == "imax" and re.search(r"\d{1,2}:\d{2}\s*[ap]m?\b", lower):
                matches.append(nearby_line)

    return matches


def run():
    seen = load_seen()
    newly_sent = []
    all_debug_text = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            # Falls back to the bundled browser if the real Chrome
            # channel was not installed on this runner.
            browser = p.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"],
            )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        for index, (date, allowed_hour) in enumerate(TIME_WINDOWS.items()):
            url = THEATRE_URL_TEMPLATE.format(date=date)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Try to dismiss the cookie consent banner, only worth trying
            # on the first page since it usually stays dismissed after.
            if index == 0:
                try:
                    page.get_by_role(
                        "button", name=re.compile("accept|agree|close", re.I)
                    ).first.click(timeout=5000)
                except Exception:
                    pass

            try:
                page.wait_for_selector("text=/odyssey/i", timeout=15000)
            except Exception:
                pass

            page.wait_for_timeout(2000)

            # Only keep one screenshot, from the first date checked, so we
            # can see whether the page rendered without using up too much
            # space in the debug artifact.
            if index == 0:
                page.screenshot(path="debug_screenshot.png", full_page=True)

            page_text = page.inner_text("body")
            all_debug_text.append(f"--- DATE: {date} ---\n{page_text}\n")

            for time_text in find_matches(page_text):
                hour = parse_hour(time_text)
                if hour is None:
                    continue
                if allowed_hour is not None and hour < allowed_hour:
                    continue

                key = f"{date} {time_text}"
                if key in seen:
                    continue

                send_telegram(
                    "The Odyssey, IMAX\n"
                    f"AMC Lincoln Square 13\n"
                    f"{date} at {time_text}\n\n"
                    f"{url}"
                )
                seen.add(key)
                newly_sent.append(key)

        browser.close()

    with open("debug_page_text.txt", "w") as f:
        f.write("\n".join(all_debug_text))

    save_seen(seen)

    if newly_sent:
        print(f"Sent {len(newly_sent)} new alert(s): {newly_sent}")
    else:
        print("No new matching showtimes this run.")


if __name__ == "__main__":
    run()
