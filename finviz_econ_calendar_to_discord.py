#!/usr/bin/env python3
"""
Post Finviz's economic calendar to a Discord channel via a webhook.

Finviz's calendar page renders its table with client-side JavaScript, so a
plain HTTP scrape (requests / finvizfinance) cannot see the real data — the
server sends back an empty shell. This script uses Playwright to drive a
real headless browser, load the page properly, and read the rendered table.

Setup:
    pip install playwright requests python-dateutil beautifulsoup4
    playwright install --with-deps chromium

Usage:
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxxx/yyyy"
    python finviz_econ_calendar_to_discord.py

Optional flags:
    --date YYYY-MM-DD          Only include events for this date (default: today)
    --impact low,medium,high   Only include these impact levels (default: all
                                recognized levels; unrecognized/blank impact
                                labels are always kept, never silently dropped)
    --webhook URL               Pass the webhook URL directly instead of using
                                 the DISCORD_WEBHOOK_URL environment variable

Debugging:
    Set DEBUG=1 as an environment variable to print the rendered page's
    table count and a sample of the parsed rows to stderr.

Fail-safe design: if the day-header text can't be confidently parsed as a
date, this script does NOT silently report "no events" — it falls back to
showing the whole unfiltered window instead, so a parsing hiccup can never
look identical to a genuinely quiet day.
"""

import argparse
import os
import re
import sys
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

IMPACT_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
KNOWN_IMPACTS = {"low", "medium", "high"}
EMBED_DESCRIPTION_LIMIT = 4096  # Discord's hard limit per embed description
DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")


def debug_print(*args):
    if DEBUG:
        print(*args, file=sys.stderr)


def parse_args():
    p = argparse.ArgumentParser(description="Post the Finviz economic calendar to Discord")
    p.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    p.add_argument("--impact", default="low,medium,high",
                    help="comma-separated: low,medium,high")
    p.add_argument("--webhook", default=None,
                    help="Discord webhook URL (overrides DISCORD_WEBHOOK_URL env var)")
    return p.parse_args()


def fetch_rendered_html(target_date: date) -> str:
    """Load Finviz's calendar page in a real headless browser and return
    the fully rendered HTML (after JavaScript has populated the table)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    url = f"https://finviz.com/calendar/economic?dateFrom={target_date.isoformat()}"
    debug_print(f"Loading {url} in headless Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        # "networkidle" is unreliable on sites with live tickers/ads that
        # never stop making background requests — wait for real content
        # (DOM parsed, then the actual table) instead of network silence.
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            # A generic <table> can appear elsewhere on the page (nav, footer,
            # pricing, etc.) before the real calendar rows finish loading.
            # Wait for something only real event rows would contain: a
            # time like "11:30 AM" actually rendered in the page text.
            page.wait_for_function(
                "() => /\\d{1,2}:\\d{2}\\s*[AP]M/.test(document.body.innerText)",
                timeout=20000,
            )
            debug_print("Time-formatted event text detected in the rendered page.")
        except PlaywrightTimeoutError:
            debug_print("No time-formatted event text appeared within 20s — "
                        "capturing whatever HTML is present anyway so we can see why.")
        page.wait_for_timeout(2000)  # let any trailing rows finish populating
        html = page.content()
        browser.close()
    return html


def parse_calendar_html(html: str):
    """Parse the rendered calendar HTML into a list of event dicts."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    debug_print(f"Rendered page contains {len(tables)} <table> element(s)")

    rows_out = []
    for t_idx, table in enumerate(tables):
        trs = table.find_all("tr")
        if not trs:
            continue
        header_cell = trs[0].find("td")
        if not header_cell:
            continue
        day_str = header_cell.get_text(strip=True)

        for tr in trs[1:]:
            cols = tr.find_all("td")
            if len(cols) < 6:
                continue
            time_str = cols[0].get_text(strip=True)
            release = cols[1].get_text(strip=True)

            impact = ""
            impact_img = cols[2].find("img") if len(cols) > 2 else None
            if impact_img and impact_img.get("src"):
                m = re.search(r"impact_(\w+)\.\w+", impact_img["src"])
                if m:
                    impact = m.group(1).lower()
            if not impact:
                # fall back to any class/title hint on the cell itself
                impact = (cols[2].get("title") or "").strip().lower() if len(cols) > 2 else ""

            for_field = cols[3].get_text(strip=True) if len(cols) > 3 else ""
            actual = cols[4].get_text(strip=True) if len(cols) > 4 else ""
            expected = cols[5].get_text(strip=True) if len(cols) > 5 else ""
            prior = cols[6].get_text(strip=True) if len(cols) > 6 else ""

            rows_out.append({
                "day_str": day_str,
                "time_str": time_str,
                "Release": release,
                "Impact": impact,
                "For": for_field,
                "Actual": actual,
                "Expected": expected,
                "Prior": prior,
            })

        if DEBUG and rows_out:
            debug_print(f"--- table[{t_idx}] day_str={day_str!r}, "
                        f"{len(trs) - 1} row(s) ---")
            for r in rows_out[-min(3, len(trs) - 1):]:
                debug_print(f"  time={r['time_str']!r} release={r['Release']!r} "
                            f"impact={r['Impact']!r} actual={r['Actual']!r} "
                            f"expected={r['Expected']!r} prior={r['Prior']!r}")

    return rows_out


def parse_day_string(s, year_hint):
    """Fuzzy-parse a day-header string like 'Mon Sep 14' into a date."""
    try:
        parsed = date_parser.parse(s, fuzzy=True, default=datetime(year_hint, 1, 1))
        return parsed.date()
    except (ValueError, OverflowError, TypeError):
        return None


def filter_by_date(rows, target_date: date):
    if not rows:
        return rows

    parsed = [(r, parse_day_string(r["day_str"], target_date.year)) for r in rows]
    if all(d is None for _, d in parsed):
        print(
            "WARNING: could not parse any calendar day headers; showing all "
            f"fetched rows instead of risking a false 'no events'. Sample: "
            f"{[r['day_str'] for r in rows[:5]]}",
            file=sys.stderr,
        )
        return rows

    return [r for r, d in parsed if d == target_date]


def keep_row_by_impact(impact_value, impacts_wanted):
    """Only exclude a row if its impact is a KNOWN level the user excluded.
    Unrecognized/blank impact labels are always kept rather than dropped."""
    v = str(impact_value).strip().lower()
    if v in KNOWN_IMPACTS and v not in impacts_wanted:
        return False
    return True


def build_embed(rows, target_date):
    title = f"📅 Economic Calendar — {target_date.strftime('%A, %B %d, %Y')}"

    if not rows:
        return {
            "title": title,
            "description": "No matching releases for this date.",
            "color": 0x2ECC71,
        }

    lines = []
    for row in sorted(rows, key=lambda r: r["time_str"]):
        impact = str(row.get("Impact", "")).strip().lower()
        emoji = IMPACT_EMOJI.get(impact, "⚪")
        actual = row["Actual"] or "—"
        expected = row["Expected"] or "—"
        prior = row["Prior"] or "—"
        lines.append(
            f"**{row['time_str']}** {emoji} {row['Release']}  _({row['For']})_\n"
            f"> Actual: `{actual}`  Expected: `{expected}`  Prior: `{prior}`"
        )

    description = "\n\n".join(lines)
    if len(description) > EMBED_DESCRIPTION_LIMIT:
        description = description[: EMBED_DESCRIPTION_LIMIT - 20] + "\n… (truncated)"

    return {
        "title": title,
        "description": description,
        "color": 0x3498DB,
        "footer": {"text": "Source: finviz.com/calendar"},
        "timestamp": datetime.utcnow().isoformat(),
    }


def post_to_discord(webhook_url, embed):
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    args = parse_args()
    webhook_url = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("Set the DISCORD_WEBHOOK_URL environment variable or pass --webhook")

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    )
    impacts = {i.strip().lower() for i in args.impact.split(",") if i.strip()}

    html = fetch_rendered_html(target_date)
    rows = parse_calendar_html(html)
    rows = filter_by_date(rows, target_date)
    rows = [r for r in rows if keep_row_by_impact(r["Impact"], impacts)]

    embed = build_embed(rows, target_date)
    post_to_discord(webhook_url, embed)
    print(f"Posted {len(rows)} event(s) for {target_date} to Discord.")


if __name__ == "__main__":
    main()
