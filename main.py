#!/usr/bin/env python3
"""Claude Usage Curses Monitor - Terminal UI for Claude plan usage limits."""

import curses
import json
import locale
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Enable UTF-8 support in curses
locale.setlocale(locale.LC_ALL, '')

# Window durations in seconds
WINDOW_DURATIONS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 24 * 3600,
    "seven_day_opus": 7 * 24 * 3600,
    "seven_day_sonnet": 7 * 24 * 3600,
}

DISPLAY_NAMES = {
    "five_hour": "Current session",
    "seven_day": "All models (7-day)",
    "seven_day_opus": "Opus only (7-day)",
    "seven_day_sonnet": "Sonnet only (7-day)",
}

# Ordered display preference
CATEGORY_ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]

FILL_CHAR = " "
EMPTY_CHAR = " "
MARKER_CHAR = "|"

# Refresh intervals in seconds
REFRESH_FOCUSED = 30
REFRESH_UNFOCUSED = 600


def get_access_token():
    """Retrieve Claude access token from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None, "Failed to read keychain: " + result.stderr.strip()
        creds = json.loads(result.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            return None, "No accessToken found in keychain credentials"
        return token, None
    except json.JSONDecodeError:
        return None, "Failed to parse keychain credentials as JSON"
    except FileNotFoundError:
        return None, "'security' command not found (macOS only)"
    except subprocess.TimeoutExpired:
        return None, "Keychain access timed out"
    except Exception as e:
        return None, f"Keychain error: {e}"


def fetch_usage(token):
    """Fetch usage data from the Claude API."""
    url = "https://api.anthropic.com/api/oauth/usage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data, None
    except urllib.error.HTTPError as e:
        return None, f"API error: {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except Exception as e:
        return None, f"Fetch error: {e}"


def parse_usage(data):
    """Parse API response into display-ready category list."""
    categories = []
    seen = set()
    for key in CATEGORY_ORDER:
        if key not in data:
            continue
        seen.add(key)
        entry = data[key]
        if not isinstance(entry, dict):
            continue
        utilization = entry.get("utilization")
        resets_at = entry.get("resets_at")
        if utilization is None and resets_at is None:
            continue
        if utilization is None:
            utilization = 0.0
        categories.append({
            "key": key,
            "name": DISPLAY_NAMES.get(key, key),
            "utilization": utilization,
            "resets_at": resets_at,
            "window_seconds": WINDOW_DURATIONS.get(key, 5 * 3600),
        })
    # Handle unknown keys that look like usage categories
    for key, entry in data.items():
        if key in seen or not isinstance(entry, dict):
            continue
        utilization = entry.get("utilization")
        resets_at = entry.get("resets_at")
        if utilization is None and resets_at is None:
            continue
        if utilization is None:
            utilization = 0.0
        window = WINDOW_DURATIONS.get(key, 7 * 24 * 3600)
        categories.append({
            "key": key,
            "name": DISPLAY_NAMES.get(key, key.replace("_", " ").title()),
            "utilization": utilization,
            "resets_at": resets_at,
            "window_seconds": window,
        })
    return categories


def calc_glide_slope(resets_at_str, window_seconds):
    """Calculate glide slope percentage (how much time has elapsed in the window)."""
    if not resets_at_str:
        return 0.0
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    now = datetime.now(timezone.utc)
    remaining = (resets_at - now).total_seconds()
    elapsed = window_seconds - remaining
    if window_seconds <= 0:
        return 0.0
    pct = elapsed / window_seconds * 100
    return max(0.0, min(100.0, pct))


def format_reset_time(resets_at_str):
    """Format reset time as human-readable string."""
    if not resets_at_str:
        return ""
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    now = datetime.now(timezone.utc)
    remaining = (resets_at - now).total_seconds()
    if remaining <= 0:
        return "Resetting now"
    if remaining < 3600:
        mins = int(remaining / 60)
        return f"Resets in {mins} min"
    if remaining < 24 * 3600:
        hrs = int(remaining / 3600)
        mins = int((remaining % 3600) / 60)
        if mins > 0:
            return f"Resets in {hrs} hr {mins} min"
        return f"Resets in {hrs} hr"
    # Show day and time
    local_reset = resets_at.astimezone()
    day = local_reset.strftime("%a")
    hour = local_reset.hour % 12
    if hour == 0:
        hour = 12
    ampm = "AM" if local_reset.hour < 12 else "PM"
    minute = local_reset.strftime("%M")
    return f"Resets {day} {hour}:{minute} {ampm}"


def format_updated_ago(last_fetch_time):
    """Format 'Updated: ...' string."""
    if last_fetch_time is None:
        return "Updated: never"
    elapsed = time.time() - last_fetch_time
    if elapsed < 5:
        return "Updated: just now"
    if elapsed < 60:
        return f"Updated: {int(elapsed)}s ago"
    if elapsed < 3600:
        return f"Updated: {int(elapsed / 60)}m ago"
    return f"Updated: {int(elapsed / 3600)}h ago"


def init_colors():
    """Initialize curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    # Pair 1: Blue background (usage within glide slope)
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
    # Pair 2: Yellow background (usage exceeding glide slope)
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    # Pair 3: Dim empty portion (dark gray background)
    curses.init_pair(3, -1, curses.COLOR_BLACK)
    # Pair 4: Glide slope marker on blue bg (when over glide slope)
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
    # Pair 5: Normal text
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    # Pair 6: Title/header
    curses.init_pair(6, curses.COLOR_CYAN, -1)
    # Pair 7: Error text
    curses.init_pair(7, curses.COLOR_RED, -1)
    # Pair 8: Glide slope marker on black bg (when under glide slope)
    curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLACK)


def draw_bar(win, y, x, width, usage_pct, glide_pct):
    """Draw a single usage bar with glide slope marker."""
    if width < 3:
        return
    max_h, max_w = win.getmaxyx()
    if y >= max_h:
        return
    # Clamp to available width
    avail = max_w - x
    if avail < 3:
        return
    width = min(width, avail)

    usage_pos = int(round(usage_pct / 100 * width))
    usage_pos = max(0, min(width, usage_pos))
    glide_pos = int(round(glide_pct / 100 * width))
    glide_pos = max(0, min(width - 1, glide_pos))

    for i in range(width):
        if i >= avail:
            break
        # Don't write to bottom-right corner
        if y == max_h - 1 and x + i == max_w - 1:
            break

        is_marker = (i == glide_pos)
        is_filled = (i < usage_pos)

        if is_marker:
            if usage_pct > glide_pct:
                # Over glide slope: marker blends with blue (left side)
                attr = curses.color_pair(4) | curses.A_BOLD
            else:
                # Under glide slope: marker blends with black (right side)
                attr = curses.color_pair(8) | curses.A_BOLD
            try:
                win.addstr(y, x + i, MARKER_CHAR, attr)
            except curses.error:
                pass
        elif is_filled:
            if usage_pct > glide_pct and i >= glide_pos:
                # Over glide slope - yellow
                attr = curses.color_pair(2)
            else:
                # Within glide slope - blue
                attr = curses.color_pair(1)
            try:
                win.addstr(y, x + i, FILL_CHAR, attr)
            except curses.error:
                pass
        else:
            try:
                win.addstr(y, x + i, EMPTY_CHAR, curses.color_pair(3))
            except curses.error:
                pass


def draw_ui(win, categories, last_fetch_time, error_msg):
    """Draw the full UI."""
    win.erase()
    max_h, max_w = win.getmaxyx()
    if max_h < 3 or max_w < 20:
        try:
            win.addstr(0, 0, "Terminal too small")
        except curses.error:
            pass
        win.noutrefresh()
        return

    margin = 2
    content_width = max_w - margin * 2

    # Header
    title = "Claude Usage Monitor"
    updated = format_updated_ago(last_fetch_time)
    row = 0
    try:
        win.addstr(row, margin, title, curses.color_pair(6) | curses.A_BOLD)
        if len(updated) < content_width - len(title) - 2:
            win.addstr(row, max_w - margin - len(updated), updated, curses.color_pair(5))
    except curses.error:
        pass
    row += 2

    # Error message
    if error_msg:
        try:
            err_display = error_msg[:content_width]
            win.addstr(row, margin, err_display, curses.color_pair(7))
        except curses.error:
            pass
        row += 2

    # Categories
    if not categories and not error_msg:
        try:
            win.addstr(row, margin, "No usage data available", curses.color_pair(5))
        except curses.error:
            pass
        row += 1

    for cat in categories:
        if row + 3 >= max_h - 2:
            break

        usage = cat["utilization"]
        glide = calc_glide_slope(cat["resets_at"], cat["window_seconds"])
        reset_str = format_reset_time(cat["resets_at"])

        # Line 1: name and usage %
        name = cat["name"]
        usage_str = f"{usage:.0f}% used"
        try:
            win.addstr(row, margin, name, curses.color_pair(5) | curses.A_BOLD)
            if len(usage_str) < content_width - len(name):
                win.addstr(row, max_w - margin - len(usage_str), usage_str, curses.color_pair(5))
        except curses.error:
            pass
        row += 1

        # Line 2: reset time
        if reset_str:
            try:
                win.addstr(row, margin, reset_str, curses.color_pair(5) | curses.A_DIM)
            except curses.error:
                pass
        row += 1

        # Line 3: bar
        draw_bar(win, row, margin, content_width, usage, glide)
        row += 2

    # Footer
    footer = "q: quit  r: refresh"
    footer_row = max_h - 1
    if footer_row > row:
        try:
            footer_x = max_w - margin - len(footer)
            if footer_x >= margin:
                win.addstr(footer_row, footer_x, footer, curses.color_pair(5) | curses.A_DIM)
        except curses.error:
            pass

    win.noutrefresh()


def main(stdscr):
    """Main curses application loop."""
    # Curses setup
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(1000)  # 1 second timeout for getch
    init_colors()

    # Enable focus reporting
    sys.stdout.write("\033[?1004h")
    sys.stdout.flush()

    has_focus = True
    categories = []
    last_fetch_time = None
    last_fetch_attempt = 0
    error_msg = None
    escape_buf = []
    escape_time = 0

    def do_fetch():
        nonlocal categories, last_fetch_time, last_fetch_attempt, error_msg
        last_fetch_attempt = time.time()
        token, err = get_access_token()
        if err:
            error_msg = err
            return
        data, err = fetch_usage(token)
        if err:
            error_msg = err
            return
        error_msg = None
        categories = parse_usage(data)
        last_fetch_time = time.time()

    # Initial fetch
    do_fetch()

    try:
        while True:
            draw_ui(stdscr, categories, last_fetch_time, error_msg)
            curses.doupdate()

            ch = stdscr.getch()

            # Handle escape sequences for focus events
            if ch == 27:  # ESC
                escape_buf = [27]
                escape_time = time.time()
            elif escape_buf:
                escape_buf.append(ch)
                if len(escape_buf) == 2 and escape_buf[0] == 27 and ch == 91:
                    # Got ESC [, wait for next char
                    pass
                elif len(escape_buf) == 3 and escape_buf[0] == 27 and escape_buf[1] == 91:
                    if ch == 73:  # 'I' - focus in
                        has_focus = True
                    elif ch == 79:  # 'O' - focus out
                        has_focus = False
                    escape_buf = []
                else:
                    escape_buf = []
            elif ch == ord("q") or ch == ord("Q"):
                break
            elif ch == ord("r") or ch == ord("R"):
                do_fetch()
            elif ch == curses.KEY_RESIZE:
                stdscr.clear()

            # Clear stale escape buffer
            if escape_buf and time.time() - escape_time > 0.5:
                escape_buf = []

            # Check if refresh needed
            now = time.time()
            if last_fetch_time is not None:
                interval = REFRESH_FOCUSED if has_focus else REFRESH_UNFOCUSED
                if now - last_fetch_time >= interval:
                    do_fetch()
            elif error_msg and now - last_fetch_attempt >= 10:
                # Retry failed fetch every 10 seconds
                do_fetch()

    finally:
        # Disable focus reporting
        sys.stdout.write("\033[?1004l")
        sys.stdout.flush()


if __name__ == "__main__":
    curses.wrapper(main)
