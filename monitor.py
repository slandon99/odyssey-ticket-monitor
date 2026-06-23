"""
Odyssey IMAX ticket monitor for AMC Lincoln Square 13.

What this does, in plain terms:
1. Opens the AMC showtimes page in a real headless browser (Playwright),
   so JavaScript loaded content actually shows up, unlike a plain
   requests.get call.
2. Looks at the date tabs on that page and only clicks into the ones
   that match a date you actually care about (see TIME_WINDOWS below).
3. On each matching date, scans the rendered text for "Odyssey" and
   checks whether an IMAX showtime is listed under it.
4. Compares any IMAX showtime found against your time window rule for
   that date.
5. Sends one Telegram alert per matching showtime, only once ever,
   using seen_showtimes.json to remember what has already been sent.
6. Always saves a screenshot and the full rendered page text as debug
   files, so if the parsing logic is wrong, we can see exactly what the
   real page looked like and fix it quickly.

Known limitation: the part of this script that finds "Odyssey" and
"IMAX" text on the page (see find_matches below) was written without
being able to load the real AMC page directly. It is a reasonable best
attempt, not something already confirmed against the live site. Check
the debug_page_text.txt artifact after the first real run. If nothing
matches when it should, that file will show why.
"""

import json
import os
import re
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

THEATRE_URL = "https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13/showtimes/all"

MOVIE_KEYWORD = "odyssey"
FORMAT_KEYWORDS = ["imax", "dolby cinema", "standard", "real d 3d", "prime", "dine-in"]

SEEN_FILE = "seen_showtimes.json"

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

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


def guess_date_from_label(label):
    """Turn a date tab label like 'Thu Jul 16' into '2026-07-16'."""
    match = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", label)
    if not match:
        return None
    month_text, day_text = match.groups()
    month_key = month_text.lower()[:3]
    if month_key not in MONTH_MAP:
        return None
    month = MONTH_MAP[month_key]
    day = int(day_text)
    return f"2026-{month:02d}-{day:02d}"


def parse_hour(time_text):
    """Turn '7:40pm' or '7:40 PM' into an hour on a 24 hour clock."""
    match = re.search(r"(\d{1,2}):(\d{2})\s*([ap]m)", time_text.lower())
    if not match:
        return None
    hour, _minute, meridian = match.groups()
    hour = int(hour)
    if meridian == "pm" and hour != 12:
        hour += 12
    if meridian == "am" and hour == 12:
        hour = 0
    return hour


def find_matches(page_text):
    """
    Scan the rendered page text for IMAX showtimes of The Odyssey.
    Returns a list of time strings found under an IMAX heading near
    an Odyssey mention.

    This is the part most likely to need adjusting after seeing the
    real debug_page_text.txt output.
    """
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    matches = []

    for i, line in enumerate(lines):
        if MOVIE_KEYWORD not in line.lower():
            continue

        current_format = None
        # Look at the next 30 lines after the Odyssey title for format
        # labels and showtime stamps.
        for nearby_line in lines[i + 1: i + 31]:
            lower = nearby_line.lower()

            matched_format = None
            for fmt in FORMAT_KEYWORDS:
                if fmt in lower:
                    matched_format = fmt
                    break
            if matched_format:
                current_format = matched_format
                continue

            if current_format == "imax" and re.search(r"\d{1,2}:\d{2}\s*[ap]m", lower):
                matches.append(nearby_line)

    return matches


def run():
    seen = load_seen()
    newly_sent = []

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
        page.goto(THEATRE_URL, wait_until="networkidle", timeout=60000)

        # Try to dismiss the cookie consent banner, since some sites pause
        # their main content scripts until it is closed. This is wrapped
        # in a try block because the banner might not always appear.
        try:
            page.get_by_role(
                "button", name=re.compile("accept|agree|close", re.I)
            ).first.click(timeout=5000)
        except Exception:
            pass

        # Give the showtimes widget real time to load real content instead
        # of guessing a fixed delay. Falls through either way so debug
        # files still get written if this never appears.
        try:
            page.wait_for_selector("text=/imax/i", timeout=15000)
        except Exception:
            pass

        page.wait_for_timeout(3000)

        # Always save debug info so we can check what the page actually
        # looked like, regardless of whether anything matched.
        page.screenshot(path="debug_screenshot.png", full_page=True)

        date_tab_candidates = page.get_by_role("button")
        tab_count = date_tab_candidates.count()

        all_debug_text = []

        for i in range(tab_count):
            tab = date_tab_candidates.nth(i)
            try:
                tab_text = tab.inner_text(timeout=2000).strip()
            except Exception:
                continue

            tab_date = guess_date_from_label(tab_text)
            if tab_date is None or tab_date not in TIME_WINDOWS:
                continue

            try:
                tab.click(timeout=5000)
                page.wait_for_timeout(2000)
            except Exception:
                continue

            page_text = page.inner_text("body")
            all_debug_text.append(f"--- DATE TAB: {tab_text} ({tab_date}) ---\n{page_text}\n")

            allowed_hour = TIME_WINDOWS[tab_date]
            for time_text in find_matches(page_text):
                hour = parse_hour(time_text)
                if hour is None:
                    continue
                if allowed_hour is not None and hour < allowed_hour:
                    continue

                key = f"{tab_date} {time_text}"
                if key in seen:
                    continue

                send_telegram(
                    "The Odyssey, IMAX\n"
                    f"AMC Lincoln Square 13\n"
                    f"{tab_date} at {time_text}\n\n"
                    f"{THEATRE_URL}"
                )
                seen.add(key)
                newly_sent.append(key)

        fallback_text = page.inner_text("body")
        browser.close()

        with open("debug_page_text.txt", "w") as f:
            if all_debug_text:
                f.write("\n".join(all_debug_text))
            else:
                f.write(
                    "No date tabs matched any of our target dates this run.\n"
                    "Full page text below for reference:\n\n" + fallback_text
                )

    save_seen(seen)

    if newly_sent:
        print(f"Sent {len(newly_sent)} new alert(s): {newly_sent}")
    else:
        print("No new matching showtimes this run.")


if __name__ == "__main__":
    run()
