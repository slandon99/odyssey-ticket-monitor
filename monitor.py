"""
Odyssey IMAX ticket monitor for AMC Lincoln Square 13.

What this does, in plain terms:
1. Opens Fandango's page for this theater in a real browser (Playwright),
   once per date you care about, using a date parameter directly in the
   URL.
2. Reads the real structured showtime data that Fandango embeds in the
   page's HTML (not just the visible text), which includes an exact
   available or sold out status, the exact format of each showing, and
   a direct booking link, per showtime.
3. Keeps only showtimes that are IMAX and fall inside your time window
   rule for that date.
4. Sends a Telegram alert whenever a showtime transitions into being
   available, whether that is the first time it is seen or a reopen
   after previously selling out. A showing that stays available run
   after run only triggers one alert, not a repeat every run.
5. Always saves a screenshot and the full page HTML from the first date
   checked, plus the rendered text for every date, as debug files.

This replaced an earlier version that scanned the visible page text and
guessed at formats and times. That version could not tell a sold out
showtime from an available one, since that distinction is not visible
in plain text, only in the underlying data. This version reads the real
data directly instead of guessing.
"""

import json
import os
import re
import html as ihtml
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

THEATRE_URL_TEMPLATE = "https://www.fandango.com/amc-lincoln-square-13-aabqi/theater-page?date={date}&a=11533"

MOVIE_KEYWORD = "odyssey"

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
            data = json.load(f)
            # Older versions of this script stored a flat list of
            # already-alerted showtimes instead of a status per
            # showtime. Treat that old format as a fresh start, since
            # it has no status information to recover.
            if isinstance(data, dict):
                return data
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
    response.raise_for_status()


def extract_movie_section(page_html, keyword):
    """
    Returns the slice of the page HTML belonging to the movie whose
    card contains the given keyword, so showtimes for some other movie
    on the same page never get picked up by accident. Each movie sits
    inside its own <article class="shared-movie-showtimes__movie">
    element, immediately followed by a sibling section holding that
    movie's showtimes, ending where the next movie's article begins.
    """
    article_token = '<article class="shared-movie-showtimes__movie"'
    positions = [m.start() for m in re.finditer(re.escape(article_token), page_html)]

    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(page_html)
        section = page_html[start:end]
        # Only need to look at the first part of the section (before the
        # showtimes start) to check which movie this is.
        header = section[:1500]
        if keyword in header.lower():
            return section

    return ""


def extract_amenity_groups(section_html):
    """
    Pulls out every data-amenity-group attribute in the given HTML and
    decodes it from a JSON-as-HTML-attribute string into a real dict.
    Each one describes one format (IMAX, Standard, and so on) and lists
    every showtime in that format, including its real available or sold
    out status.
    """
    groups = []
    start_token = 'data-amenity-group="'
    pos = 0
    while True:
        idx = section_html.find(start_token, pos)
        if idx == -1:
            break
        start = idx + len(start_token)
        end = section_html.find('"', start)
        raw = section_html[start:end]
        pos = end + 1

        decoded = ihtml.unescape(raw)
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        groups.append(data)

    return groups


def find_matches(page_html):
    """
    Returns a list of dicts, one per IMAX showtime of The Odyssey found
    in the page's embedded data, available or sold out:
    {"time": "10:00p", "hour": 22, "url": "...", "id": "...", "status": "available"}
    """
    section = extract_movie_section(page_html, MOVIE_KEYWORD)
    if not section:
        return []

    matches = []
    for group in extract_amenity_groups(section):
        for showtime in group.get("showtimes", []):
            if showtime.get("expired"):
                continue

            film_formats = [
                fmt.get("filterName", "").lower()
                for fmt in showtime.get("filmFormat", [])
            ]
            if not any("imax" in fmt for fmt in film_formats):
                continue

            ticketing_date = showtime.get("ticketingDate", "")
            hour_match = re.search(r"\+(\d{1,2}):(\d{2})", ticketing_date)
            hour = int(hour_match.group(1)) if hour_match else None

            matches.append({
                "time": showtime.get("date"),
                "hour": hour,
                "url": showtime.get("ticketingJumpPageURL"),
                "id": showtime.get("id") or showtime.get("showtimeHashCode"),
                "status": showtime.get("type"),
            })

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

            page_html = page.content()

            # Save HTML for every date while we are actively debugging
            # specific dates, so any date in question can be inspected
            # directly instead of guessing.
            page.screenshot(path=f"debug_screenshot_{date}.png", full_page=True)
            with open(f"debug_page_html_{date}.html", "w") as f:
                f.write(page_html)

            all_debug_text.append(
                f"--- DATE: {date} ---\n{page.inner_text('body')}\n"
            )

            for match in find_matches(page_html):
                hour = match["hour"]
                if hour is None:
                    continue
                if allowed_hour is not None and hour < allowed_hour:
                    continue

                key = f"{date}:{match['id']}"
                current_status = match["status"]
                previous_status = seen.get(key)

                if current_status == "available" and previous_status != "available":
                    send_telegram(
                        "The Odyssey, IMAX, seats available\n"
                        f"AMC Lincoln Square 13\n"
                        f"{date} at {match['time']}\n\n"
                        f"{match['url']}"
                    )
                    newly_sent.append(key)

                seen[key] = current_status

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
