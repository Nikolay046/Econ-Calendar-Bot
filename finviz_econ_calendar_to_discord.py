#!/usr/bin/env python3
"""
Post Finviz's economic calendar to a Discord channel via a webhook.

Setup:
    pip install finvizfinance requests python-dateutil

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
    Set DEBUG=1 as an environment variable to print the raw scraped rows
    (day/time/impact/release) to stderr, so you can see exactly what Finviz
    returned if something looks wrong.

Note: Finviz's calendar page only exposes the CURRENT live calendar window
(roughly the current week) — there's no way to pull an arbitrary past/future
date on demand. --date filters within whatever is currently being shown; if
you ask for a date outside that window you'll get an empty result.

Fail-safe design: if the day-header text from Finviz can't be confidently
parsed as a date at all (e.g. the site's HTML format changes), this script
does NOT silently report "no events" — it falls back to showing the whole
unfiltered window instead, so a parsing hiccup can never look identical to
a genuinely quiet day.
"""

import argparse
import os
import sys
from datetime import datetime, date

import requests
from dateutil import parser as date_parser
from finvizfinance.calendar import Calendar

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


def fetch_calendar_df():
    """Pull the live calendar table from Finviz as a DataFrame."""
    cal = Calendar()
    df = cal.calendar()
    if df.empty:
        return df
    # "Datetime" looks like "Monday Sep 14, 08:30AM" -> split into day / time
    split = df["Datetime"].str.split(",", n=1, expand=True)
    df["day_str"] = split[0].str.strip()
    df["time_str"] = split[1].str.strip() if split.shape[1] > 1 else ""

    if DEBUG:
        debug_print("---- RAW CALENDAR ROWS ----")
        for _, r in df.iterrows():
            debug_print(f"day_str={r['day_str']!r}  time_str={r['time_str']!r}  "
                         f"Impact={r.get('Impact')!r}  Release={r.get('Release')!r}")
        debug_print("---------------------------")

    return df


def parse_day_string(s, year_hint):
    """Fuzzy-parse a day-header string like 'Monday Sep 14' into a date.

    Uses dateutil's fuzzy parser instead of a rigid format string, since we
    can't be 100% sure of Finviz's exact wording/abbreviations, and a mismatch
    there must never be mistaken for 'no events'.
    """
    try:
        parsed = date_parser.parse(s, fuzzy=True, default=datetime(year_hint, 1, 1))
        return parsed.date()
    except (ValueError, OverflowError, TypeError):
        return None


def filter_by_date(df, target_date: date):
    if df.empty:
        return df

    df = df.copy()
    df["parsed_date"] = df["day_str"].apply(lambda s: parse_day_string(s, target_date.year))

    if df["parsed_date"].isna().all():
        # Couldn't parse ANY day header — don't pretend that means "no events".
        print(
            "WARNING: could not parse any calendar day headers; showing the "
            "full unfiltered window instead of risking a false 'no events'. "
            f"Sample raw values: {list(df['day_str'].unique()[:5])}",
            file=sys.stderr,
        )
        return df

    return df[df["parsed_date"] == target_date]


def keep_row_by_impact(impact_value, impacts_wanted):
    """Only exclude a row if its impact is a KNOWN level the user excluded.
    Unrecognized/blank impact labels are always kept rather than dropped."""
    v = str(impact_value).strip().lower()
    if v in KNOWN_IMPACTS and v not in impacts_wanted:
        return False
    return True


def build_embed(df, target_date):
    title = f"📅 Economic Calendar — {target_date.strftime('%A, %B %d, %Y')}"

    if df.empty:
        return {
            "title": title,
            "description": "No matching releases for this date.",
            "color": 0x2ECC71,
        }

    lines = []
    for _, row in df.sort_values("time_str").iterrows():
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

    df = fetch_calendar_df()
    df = filter_by_date(df, target_date)
    if not df.empty:
        df = df[df["Impact"].apply(lambda v: keep_row_by_impact(v, impacts))]

    embed = build_embed(df, target_date)
    post_to_discord(webhook_url, embed)
    print(f"Posted {len(df)} event(s) for {target_date} to Discord.")


if __name__ == "__main__":
    main()
