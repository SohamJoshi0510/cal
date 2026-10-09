from __future__ import annotations

import argparse
import calendar
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from rich.columns import Columns
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import readchar
except ImportError:
    readchar = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

console = Console()

# Absolute path relative to where cal2.py lives
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "events.json"
DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]  # Monday -> Sunday


class Palette(NamedTuple):
    accent: str     # title, headers, border
    today: str      # today's date
    selected: str   # day picked with -d
    weekend: str    # Sa / Su
    event: str      # days with scheduled events


PALETTES: dict[str, Palette] = {
    "cyan":    Palette("turquoise2",   "bold bright_yellow", "bold bright_green",   "indian_red",    "bold bright_magenta"),
    "magenta": Palette("orchid1",      "bold bright_cyan",   "bold bright_yellow",  "light_coral",   "bold bright_green"),
    "green":   Palette("spring_green3", "bold bright_yellow", "bold bright_magenta", "salmon1",       "bold bright_cyan"),
    "blue":    Palette("dodger_blue2",  "bold orange1",       "bold bright_cyan",    "light_coral",   "bold bright_yellow"),
    "yellow":  Palette("gold1",        "bold bright_cyan",   "bold bright_magenta", "orange_red1",   "bold bright_green"),
}

THEMES = list(PALETTES) + ["random"]


# --- Speech ---
# Each announcement runs in its own short-lived process with a fresh pyttsx3
# engine. A persistent engine + runAndWait() is unreliable on Windows (SAPI5):
# after the first utterance it can silently swallow later ones, which is what
# made some events go "unread" while cycling. A fresh process per utterance
# can't get into that state, and starting a new announcement simply cuts off
# the previous one instead of piling up a stale queue.

_speech_proc: subprocess.Popen | None = None

_TTS_SNIPPET = (
    "import sys, pyttsx3\n"
    "engine = pyttsx3.init()\n"
    "engine.say(sys.argv[1])\n"
    "engine.runAndWait()\n"
)

TTS_MISSING = "pyttsx3 not installed. Run: pip install pyttsx3"


def stop_speech() -> None:
    """Cut off whatever is currently being spoken."""
    global _speech_proc
    proc, _speech_proc = _speech_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def speak_text_async(text: str) -> None:
    """Speak text in the background, replacing any announcement in progress."""
    global _speech_proc
    if pyttsx3 is None:
        return
    stop_speech()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        _speech_proc = subprocess.Popen(
            [sys.executable, "-c", _TTS_SNIPPET, text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError:
        _speech_proc = None


def get_random_palette() -> Palette:
    """Generate a dynamic random palette on each execution."""
    return Palette(
        random.choice(["turquoise2", "orchid1", "spring_green3", "dodger_blue2", "gold1"]),
        random.choice(["bold bright_yellow", "bold bright_cyan", "bold orange1"]),
        random.choice(["bold bright_green", "bold bright_yellow", "bold bright_magenta", "bold bright_cyan"]),
        random.choice(["indian_red", "light_coral", "salmon1", "orange_red1"]),
        random.choice(["bold bright_magenta", "bold bright_green", "bold bright_cyan", "bold bright_yellow"]),
    )


# --- Database Structures & Helpers ---

def load_db(events_path: Path) -> dict[str, Any]:
    if not events_path.exists():
        return {"events": []}

    try:
        with open(events_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "events" not in data:
            migrated_events = []
            for date_str, ev_list in data.items():
                for ev_title in ev_list:
                    migrated_events.append({
                        "id": f"ev_{int(time.time()*1000)}_{random.randint(100, 999)}",
                        "title": ev_title,
                        "start_date": date_str,
                        "recurrence": "none",
                        "exceptions": [],
                    })
            data = {"events": migrated_events}
            save_db(events_path, data)

        return data
    except Exception as e:
        console.print(f"[bold red]Error reading event database:[/] {e}")
        return {"events": []}


def save_db(events_path: Path, data: dict[str, Any]) -> None:
    try:
        with open(events_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        console.print(f"[bold red]Error saving event database:[/] {e}")


def is_event_on_date(event: dict[str, Any], target_date: datetime) -> bool:
    start_dt = datetime.strptime(event["start_date"], "%Y-%m-%d")
    date_str = target_date.strftime("%Y-%m-%d")

    if target_date.date() < start_dt.date():
        return False

    if date_str in event.get("exceptions", []):
        return False

    rec = event.get("recurrence", "none")

    if rec == "none":
        return target_date.date() == start_dt.date()
    elif rec == "weekly":
        return target_date.weekday() == start_dt.weekday()
    elif rec == "weekly_custom":
        weekdays = event.get("weekdays", [])
        return target_date.weekday() in weekdays
    elif rec == "monthly":
        return target_date.day == start_dt.day
    elif rec == "yearly":
        return (target_date.month, target_date.day) == (start_dt.month, start_dt.day)
    elif rec == "selected_dates":
        return date_str in event.get("dates", [])
    elif rec == "date_range":
        end_date = event.get("end_date")
        return bool(end_date) and start_dt.date() <= target_date.date() <= datetime.strptime(end_date, "%Y-%m-%d").date()

    return False


def get_events_for_date(events_path: Path, target_date: datetime) -> list[dict[str, Any]]:
    db = load_db(events_path)
    return [ev for ev in db.get("events", []) if is_event_on_date(ev, target_date)]


def check_duplicate_event(events_path: Path, date_str: str, title: str) -> dict[str, Any] | None:
    """Check if an event with matching title already exists or recurs on date_str."""
    target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    existing_events = get_events_for_date(events_path, target_dt)

    for ev in existing_events:
        if ev["title"].strip().lower() == title.strip().lower():
            return ev
    return None


def get_upcoming_events(
    events_path: Path,
    from_date: datetime,
    limit: int = 5,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Return the next distinct event occurrences after/on from_date."""
    db = load_db(events_path)
    events = db.get("events", [])
    upcoming: list[tuple[datetime, dict[str, Any]]] = []

    # Look several years ahead so explicitly scheduled distant dates are included.
    for day_offset in range(366 * 5):
        check_dt = from_date + timedelta(days=day_offset)
        matching = [
            ev for ev in events
            if is_event_on_date(ev, check_dt)
        ]

        for ev in matching:
            upcoming.append((check_dt, ev))
            if len(upcoming) >= limit:
                return upcoming

    return upcoming


def get_events_map_for_period(
    events_path: Path, year: int, month: int | None = None
) -> dict[str, list[dict[str, Any]]]:
    db = load_db(events_path)
    result = {}

    start_m = month if month else 1
    end_m = month if month else 12

    for m in range(start_m, end_m + 1):
        num_days = calendar.monthrange(year, m)[1]
        for d in range(1, num_days + 1):
            curr_dt = datetime(year, m, d)
            matching = [ev for ev in db.get("events", []) if is_event_on_date(ev, curr_dt)]
            if matching:
                result[curr_dt.strftime("%Y-%m-%d")] = matching

    return result


def add_event(
    events_path: Path,
    date_str: str,
    title: str,
    recurrence: str = "none",
    weekdays: list[int] | None = None,
    dates: list[str] | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    db = load_db(events_path)
    new_event = {
        "id": f"ev_{int(time.time()*1000)}",
        "title": title,
        "start_date": date_str,
        "recurrence": recurrence,
        "exceptions": [],
    }

    if recurrence == "weekly_custom":
        new_event["weekdays"] = sorted(set(weekdays or []))
    if recurrence == "selected_dates":
        new_event["dates"] = sorted(set(dates or [date_str]))
        new_event["start_date"] = min(new_event["dates"])
    if recurrence == "date_range" and end_date:
        new_event["end_date"] = end_date

    db["events"].append(new_event)
    save_db(events_path, db)
    return new_event


def recurrence_label(event: dict[str, Any]) -> str:
    """Return a human-readable recurrence description."""
    rec = event.get("recurrence", "none")

    if rec == "selected_dates":
        return f"(dates: {len(event.get('dates', []))} selected)"
    if rec == "date_range":
        return f"(daily through {event.get('end_date', '?')})"
    if rec == "weekly_custom":
        weekdays = event.get("weekdays", [])
        names = [DAY_NAMES[d] for d in weekdays if 0 <= d <= 6]
        return f"(weekly: {', '.join(names)})" if names else "(weekly: custom)"

    return f"({rec})" if rec != "none" else ""


def _last_possible_date(event: dict[str, Any]) -> datetime | None:
    """Last day a non-repeating / bounded event can occur (None = unbounded)."""
    rec = event.get("recurrence", "none")
    try:
        if rec == "none":
            return datetime.strptime(event["start_date"], "%Y-%m-%d")
        if rec == "selected_dates":
            dates = event.get("dates", [])
            return datetime.strptime(max(dates), "%Y-%m-%d") if dates else datetime.min
        if rec == "date_range" and event.get("end_date"):
            return datetime.strptime(event["end_date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return datetime.min
    return None


def next_occurrence(
    event: dict[str, Any],
    from_date: datetime,
    horizon_days: int = 366 * 5,
) -> datetime | None:
    """First date on/after from_date on which the event occurs."""
    start = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
    last = _last_possible_date(event)

    if last is not None:
        if last.date() < start.date():
            return None  # already over; don't scan years for nothing
        span = (last.date() - start.date()).days + 1
    else:
        span = horizon_days

    for offset in range(span):
        candidate = start + timedelta(days=offset)
        if is_event_on_date(event, candidate):
            return candidate
    return None


def get_event_cycle(
    events_path: Path,
    from_date: datetime,
) -> list[tuple[datetime, dict[str, Any]]]:
    """One entry per event (its next occurrence), in chronological order.

    Listing every occurrence let a single daily/weekly event fill the whole
    cycle, so the other events never came up.
    """
    entries = []
    for ev in load_db(events_path).get("events", []):
        occurrence = next_occurrence(ev, from_date)
        if occurrence is not None:
            entries.append((occurrence, ev))
    entries.sort(key=lambda pair: (pair[0], pair[1].get("title", "").lower()))
    return entries


# --- UI Formatting Functions ---

def day_cell(day: int, style: str, bracket: bool = False, marker: str | None = None) -> Text:
    if bracket:
        label = f"[{day}]"
    elif marker:
        label = f"{day:2d}{marker}"
    else:
        label = f" {day:2d} "

    return Text(label.rjust(4) if (bracket or marker) else label, style=style)


def generate_month_table(
    year: int,
    month: int,
    highlight_day: int | None = None,
    palette: Palette = PALETTES["cyan"],
    show_title: bool = True,
    today: datetime | None = None,
    events_map: dict[str, list[dict[str, Any]]] | None = None,
) -> Table:
    today = today or datetime.now()
    events_map = events_map or {}

    table = Table(
        title=f"[bold {palette.accent}]{calendar.month_name[month]}[/]" if show_title else None,
        box=None,
        padding=(0, 0),
        show_header=True,
        title_justify="center",
        collapse_padding=True,
    )

    for index, day_name in enumerate(DAY_NAMES):
        table.add_column(
            day_name,
            justify="center",
            header_style=f"bold {palette.weekend}" if index >= 5 else f"bold {palette.accent}",
            width=4,
            no_wrap=True,
        )

    for week in calendar.monthcalendar(year, month):
        row = []
        for weekday, day in enumerate(week):
            if day == 0:
                row.append("")
                continue

            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            has_event = date_str in events_map and len(events_map[date_str]) > 0
            is_today = (year, month, day) == (today.year, today.month, today.day)

            if highlight_day is not None and day == highlight_day:
                row.append(day_cell(day, palette.selected, bracket=True))
            elif is_today:
                row.append(day_cell(day, palette.today, bracket=True))
            elif has_event:
                row.append(day_cell(day, palette.event, marker="•"))
            elif weekday >= 5:
                row.append(day_cell(day, palette.weekend))
            else:
                row.append(day_cell(day, ""))

        table.add_row(*row)

    return table


def build_events_panel(
    events_map: dict[str, list[dict[str, Any]]],
    palette: Palette = PALETTES["cyan"],
) -> Panel | None:
    if not events_map:
        return None

    table = Table(box=None, padding=(0, 2), show_header=True)
    table.add_column("Date", style=palette.event, no_wrap=True)
    table.add_column("Events", style="white")

    for d_str, ev_list in sorted(events_map.items()):
        formatted_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%b %d, %Y")
        lines = []
        for ev in ev_list:
            rec_tag = f" [dim]{escape(recurrence_label(ev))}[/dim]" if ev.get("recurrence", "none") != "none" else ""
            lines.append(f"• {escape(ev['title'])}{rec_tag}")
        table.add_row(formatted_date, "\n".join(lines))

    return Panel(
        table,
        title=f"[bold {palette.accent}]📌 Scheduled Events[/]",
        border_style=palette.accent,
        expand=False,
        padding=(0, 2),
    )


def build_interactive_layout(
    year: int,
    month: int,
    palette: Palette,
    db_path: Path,
    status_msg: str = "",
) -> Table:
    today = datetime.now()
    events_map = get_events_map_for_period(db_path, year, month)

    table = generate_month_table(
        year, month, palette=palette, show_title=False, today=today, events_map=events_map
    )

    calendar_panel = Panel(
        table,
        title=f"[bold {palette.accent}]📅 {calendar.month_name[month]} {year}[/]",
        border_style=palette.accent,
        expand=False,
        padding=(1, 2),
    )

    events_panel = build_events_panel(events_map, palette=palette)

    controls_text = (
        "[dim]Nav:[/] [bold]h/l[/] (mth) [bold]j/k[/] (yr) | "
        "[bold]a[/] Add | [bold]x[/] Delete | "
        "[bold]c[/]/[bold]b[/] Next/prev event (spoken) | [bold]u[/] Upcoming | [bold]t[/] Today | [bold]q[/] Quit"
    )

    main_layout = Table.grid(padding=(1, 0))
    main_layout.add_row(calendar_panel)
    if events_panel:
        main_layout.add_row(events_panel)
    if status_msg:
        main_layout.add_row(f"[bold green]{escape(status_msg)}[/]")
    main_layout.add_row(controls_text)

    return main_layout


def print_edit_month_context(
    year: int,
    month: int,
    palette: Palette,
    db_path: Path,
) -> None:
    """Show the current month and its scheduled events before add/delete input."""
    today = datetime.now()
    events_map = get_events_map_for_period(db_path, year, month)

    table = generate_month_table(
        year,
        month,
        palette=palette,
        show_title=False,
        today=today,
        events_map=events_map,
    )

    console.print()
    console.print(
        Panel(
            table,
            title=f"[bold {palette.accent}]📅 {calendar.month_name[month]} {year}[/]",
            border_style=palette.accent,
            expand=False,
            padding=(1, 2),
        )
    )

    events_panel = build_events_panel(events_map, palette=palette)
    if events_panel:
        console.print(events_panel)
    else:
        console.print("[dim]No events scheduled for this month.[/]")

    console.print()

def get_events_for_month(
    events_path: Path,
    year: int,
    month: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Return every event occurrence in the requested month."""
    events_map = get_events_map_for_period(events_path, year, month)
    result = []

    for date_str, events in sorted(events_map.items()):
        for event in events:
            result.append((date_str, event))

    return result


def find_events_by_title(
    events_path: Path,
    year: int,
    month: int,
    title: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Find event occurrences in the displayed month by title."""
    title_lower = title.strip().lower()

    return [
        (date_str, event)
        for date_str, event in get_events_for_month(events_path, year, month)
        if event.get("title", "").strip().lower() == title_lower
    ]


# --- Prompt helpers (add / delete flows) ---

class Cancelled(Exception):
    """Raised when the user types 'q' at any prompt."""


_WEEKDAY_LOOKUP: dict[str, int] = {}
for _i, _name in enumerate(calendar.day_name):
    _WEEKDAY_LOOKUP[_name.lower()] = _i          # monday
    _WEEKDAY_LOOKUP[_name[:3].lower()] = _i      # mon
for _i, _name in enumerate(DAY_NAMES):
    _WEEKDAY_LOOKUP[_name.lower()] = _i          # mo


def ask(label: str, default: str = "") -> str:
    """Prompt for a line. Enter accepts the default; 'q' cancels the whole flow."""
    hint = f" [dim]{escape('[' + default + ']')}[/]" if default else ""
    value = console.input(f"[bold]{label}[/]{hint}: ").strip()
    if value.lower() == "q":
        raise Cancelled
    return value or default


def ask_required(label: str) -> str:
    while True:
        value = ask(label)
        if value:
            return value
        console.print("[dim]  (can't be empty — type q to cancel)[/]")


def parse_date_input(text: str, year: int, month: int, today: date) -> date | None:
    """Flexible date entry.

    ''/today, tomorrow, a weekday name (next one, today included),
    15 (day of the month being viewed), 15/3, 15/3/2027, 2026-10-15.
    Numeric forms with a separator are day-first, except year-first ISO.
    """
    s = text.strip().lower()
    if not s or s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)

    # Explicit "next <weekday>" always means a strictly future occurrence.
    # A bare weekday means the next occurrence, including today if it matches.
    next_match = re.fullmatch(r"next\s+([a-z]+)", s)
    if next_match:
        weekday_name = next_match.group(1)
        if weekday_name in _WEEKDAY_LOOKUP:
            delta = (_WEEKDAY_LOOKUP[weekday_name] - today.weekday()) % 7
            return today + timedelta(days=delta or 7)
        return None
    if s in _WEEKDAY_LOOKUP:
        return today + timedelta(days=(_WEEKDAY_LOOKUP[s] - today.weekday()) % 7)

    try:
        nums = [int(part) for part in re.split(r"[/\-. ]+", s)]
        if len(nums) == 1:
            return date(year, month, nums[0])
        if len(nums) == 2:
            return date(year, nums[1], nums[0])
        if len(nums) == 3:
            if nums[0] > 31:                      # 2026-10-15
                return date(nums[0], nums[1], nums[2])
            return date(nums[2], nums[1], nums[0])  # 15/10/2026
    except ValueError:
        pass
    return None


def parse_weekdays(text: str) -> list[int] | None:
    """'sat,sun' or '6,7' (1 = Monday) -> sorted weekday indexes (0 = Monday)."""
    result: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip().lower()):
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= 7:
            result.add(int(part) - 1)
        elif part in _WEEKDAY_LOOKUP:
            result.add(_WEEKDAY_LOOKUP[part])
        else:
            return None
    return sorted(result) or None


def ask_date(label: str, year: int, month: int, default: str = "today") -> date:
    today = datetime.now().date()
    while True:
        raw = ask(label, default) if default else ask_required(label)
        parsed = parse_date_input(raw, year, month, today)
        if parsed:
            return parsed
        console.print("[red]  Couldn't read that date.[/]")


RECURRENCE_CHOICES = {
    "0": ("none", "Once"),
    "1": ("weekly", "Weekly (same weekday)"),
    "2": ("monthly", "Monthly (same day)"),
    "3": ("yearly", "Yearly"),
    "4": ("weekly_custom", "Certain weekdays"),
    "5": ("selected_dates", "Several specific dates"),
    "6": ("date_range", "Every day until an end date"),
}


def add_event_flow(
    db_path: Path, year: int, month: int, pal: Palette
) -> tuple[str, date | None]:
    """Calendar-first event creation with date shortcuts and a final review."""
    console.clear()
    console.print(
        f"[bold {pal.accent}]Add event[/]  "
        "[dim]Enter accepts the default · q cancels[/]"
    )
    print_edit_month_context(year, month, pal, db_path)

    try:
        title = ask_required("Event title")

        # Keep the calendar visible immediately before date entry, so the user
        # can use the displayed month instead of having to remember it.
        console.print("\n[bold cyan]1/3 · Choose a date[/]")
        print_edit_month_context(year, month, pal, db_path)
        console.print(
            "[dim]Quick dates: today · tomorrow · next Sunday · next Wednesday · "
            "sun / wed (today is allowed)\n"
            "Or enter a day number (15), day/month (15/3), or ISO date (2026-10-15).[/]"
        )
        start = ask_date("Date", year, month)
        start_str = start.strftime("%Y-%m-%d")

        dup = check_duplicate_event(db_path, start_str, title)
        if dup:
            console.print(
                f"[bold yellow]⚠ '{escape(dup['title'])}' already occurs on "
                f"{start_str} {escape(recurrence_label(dup))}[/]"
            )
            if ask("Add it anyway? (y/N)", "n").lower() != "y":
                return "Add cancelled (duplicate).", None

        console.print("\n[bold cyan]2/3 · How often does it repeat?[/]")
        for key, (_, text) in RECURRENCE_CHOICES.items():
            console.print(f"  [bold yellow]{key}[/]  {text}")
        while True:
            choice = ask("Repeat", "0")
            if choice in RECURRENCE_CHOICES:
                break
            console.print("[red]  Pick a number from the list.[/]")
        recurrence = RECURRENCE_CHOICES[choice][0]

        weekdays = dates = end_date = None

        if recurrence == "weekly_custom":
            console.print("[dim]Use weekday names (mon, wed, sun) or numbers 1–7 (Mon=1).[/]")
            while True:
                weekdays = parse_weekdays(ask("Repeat on which weekdays?"))
                if weekdays:
                    break
                console.print("[red]  Use weekday names or numbers 1–7.[/]")

        elif recurrence == "selected_dates":
            console.print(f"[dim]The start date {start_str} is already included.[/]")
            while True:
                raw = ask_required("Additional dates, comma-separated")
                parsed = [
                    parse_date_input(p, start.year, start.month, datetime.now().date())
                    for p in raw.split(",") if p.strip()
                ]
                if parsed and all(parsed):
                    dates = sorted({start_str, *(d.strftime("%Y-%m-%d") for d in parsed)})
                    break
                console.print("[red]  Couldn't read one of those dates. Try again.[/]")

        elif recurrence == "date_range":
            console.print("[dim]The event will occur every day, including the final date.[/]")
            while True:
                last = ask_date("Repeat through (inclusive)", start.year, start.month, default="")
                if last >= start:
                    end_date = last.strftime("%Y-%m-%d")
                    break
                console.print("[red]  End date must be on or after the start date.[/]")

        # Show a human-readable summary before writing to the database.
        preview = {
            "title": title,
            "start_date": start_str,
            "recurrence": recurrence,
        }
        if weekdays is not None:
            preview["weekdays"] = weekdays
        if dates is not None:
            preview["dates"] = dates
        if end_date is not None:
            preview["end_date"] = end_date

        console.print("\n[bold cyan]3/3 · Review event[/]")
        console.print(Panel(
            f"[bold]{escape(title)}[/]\n"
            f"Date: {start.strftime('%A, %B')} {start.day}, {start.year}\n"
            f"Repeat: {escape(RECURRENCE_CHOICES[choice][1])}"
            + (f"\nWeekdays: {escape(', '.join(calendar.day_name[d] for d in weekdays))}" if weekdays is not None else "")
            + (f"\nSelected dates: {len(dates)}" if dates is not None else "")
            + (f"\nThrough: {escape(end_date)}" if end_date is not None else ""),
            title="Event summary", border_style=pal.accent, expand=False,
        ))
        if ask("Save this event? (Y/n)", "y").lower() == "n":
            return "Add cancelled before saving.", None

    except (Cancelled, KeyboardInterrupt, EOFError):
        return "Add cancelled.", None

    ev = add_event(
        db_path, start_str, title,
        recurrence=recurrence, weekdays=weekdays, dates=dates, end_date=end_date,
    )
    label = recurrence_label(ev)
    status = f"✔ Added '{title}' {label} starting {ev['start_date']}".replace("  ", " ")
    return status, datetime.strptime(ev["start_date"], "%Y-%m-%d").date()


def delete_event_flow(db_path: Path, year: int, month: int, pal: Palette) -> str:
    console.clear()
    console.print(
        f"[bold {pal.accent}]Delete event ({calendar.month_name[month]} {year})[/]  "
        "[dim]q cancels[/]"
    )
    print_edit_month_context(year, month, pal, db_path)

    def describe(date_str: str, ev: dict[str, Any]) -> str:
        pretty = datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %d")
        rec = recurrence_label(ev)
        return f"{pretty} — {escape(ev['title'])} {escape(rec)}".rstrip()

    try:
        raw = ask("Day number or event name")
        if not raw:
            return "Delete cancelled."

        if raw.isdigit():
            day = int(raw)
            if not 1 <= day <= calendar.monthrange(year, month)[1]:
                return "Invalid day entered."
            target_dt = datetime(year, month, day)
            date_str = target_dt.strftime("%Y-%m-%d")
            matches = [(date_str, ev) for ev in get_events_for_date(db_path, target_dt)]
            if not matches:
                return f"No events found for {date_str}"
        else:
            matches = find_events_by_title(db_path, year, month, raw)
            if not matches:
                return f"No event named '{raw}' found in {calendar.month_name[month]} {year}."

        if len(matches) == 1:
            target_date_str, target_event = matches[0]
        else:
            console.print("\n[bold cyan]Which one?[/]")
            for i, (d, ev) in enumerate(matches, 1):
                console.print(f"  [bold yellow]{i}[/]  {describe(d, ev)}")
            sel = ask("Number")
            if not (sel.isdigit() and 1 <= int(sel) <= len(matches)):
                return "Invalid event selection."
            target_date_str, target_event = matches[int(sel) - 1]

        db = load_db(db_path)
        db_ev = next((e for e in db["events"] if e["id"] == target_event["id"]), None)
        if db_ev is None:
            return "Event no longer exists."

        if db_ev.get("recurrence", "none") == "none":
            db["events"] = [e for e in db["events"] if e["id"] != db_ev["id"]]
            save_db(db_path, db)
            return f"✔ Deleted '{db_ev['title']}'"

        console.print(
            f"\n[bold yellow]'{escape(db_ev['title'])}' is a recurring "
            f"{escape(recurrence_label(db_ev))} event.[/]"
        )
        console.print("  [bold yellow]1[/]  Delete THIS occurrence only")
        console.print("  [bold yellow]2[/]  Delete the ENTIRE series")
        scope = ask("Scope", "1")

        if scope == "2":
            db["events"] = [e for e in db["events"] if e["id"] != db_ev["id"]]
            save_db(db_path, db)
            return f"✔ Deleted entire series for '{db_ev['title']}'"
        if scope == "1":
            db_ev.setdefault("exceptions", [])
            if target_date_str not in db_ev["exceptions"]:
                db_ev["exceptions"].append(target_date_str)
            save_db(db_path, db)
            return f"✔ Removed occurrence on {target_date_str}"
        return "Invalid deletion scope."

    except (Cancelled, KeyboardInterrupt, EOFError):
        return "Delete cancelled."


# --- Speech helpers ---

def date_phrase(event_dt: datetime, now: datetime) -> str:
    """'today', 'tomorrow' or 'on Monday, October 12' (+ year if not this year)."""
    d, today = event_dt.date(), now.date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    text = f"on {event_dt:%A, %B} {event_dt.day}"
    return text if d.year == today.year else f"{text}, {d.year}"


class EventCycle:
    """Walks the upcoming events (one per event) with next / previous."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.items: list[tuple[datetime, dict[str, Any]]] = []
        self.pos = -1

    def reset(self) -> None:
        self.items, self.pos = [], -1

    def step(self, delta: int) -> tuple[int, int, datetime, dict[str, Any]] | None:
        # Rebuild when empty, and when running past the end (picks up changes
        # and a new day).
        if not self.items or (delta > 0 and self.pos + 1 >= len(self.items)):
            self.items = get_event_cycle(self.db_path, datetime.now())
            self.pos = 0 if delta < 0 else -1
            if not self.items:
                return None
        self.pos = (self.pos + delta) % len(self.items)
        dt, ev = self.items[self.pos]
        return self.pos, len(self.items), dt, ev


def speak_upcoming(db_path: Path) -> str:
    if pyttsx3 is None:
        return TTS_MISSING
    now = datetime.now()
    items = get_event_cycle(db_path, now)[:5]
    if not items:
        text = "You have no upcoming events scheduled."
    else:
        parts = [f"{ev['title']} {date_phrase(dt, now)}" for dt, ev in items]
        if len(parts) == 1:
            text = f"Your next upcoming event is {parts[0]}."
        else:
            text = "Your upcoming events are: " + "; ".join(parts) + "."
    speak_text_async(text)
    return f"🔊 {text}"


def speak_step(cycle: EventCycle, delta: int) -> str:
    if pyttsx3 is None:
        return TTS_MISSING
    step = cycle.step(delta)
    if step is None:
        text = "You have no upcoming events scheduled."
        speak_text_async(text)
        return f"🔊 {text}"
    idx, total, dt, ev = step
    now = datetime.now()
    speak_text_async(f"{ev['title']} is scheduled {date_phrase(dt, now)}.")
    return f"🔊 Event {idx + 1}/{total}: {ev['title']} — {dt:%a %b} {dt.day}"


# --- Interactive Mode ---

def run_interactive_mode(
    start_year: int,
    start_month: int,
    theme: str,
    db_path: Path,
) -> None:
    if readchar is None:
        console.print("[bold red]Please install 'readchar' to use interactive mode:[/] pip install readchar")
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print("[bold red]Interactive mode needs a real terminal (input/output is redirected).[/]")
        return

    year, month = start_year, start_month
    pal = get_random_palette() if theme == "random" else PALETTES[theme]
    status_msg = ""
    cycle = EventCycle(db_path)

    # Alternate screen: the whole UI lives in its own buffer, so nothing can
    # be left behind in the normal terminal. This replaces rich.Live, whose
    # cursor-up redraw leaves residue (and can loop) when the content is
    # taller than the window, wraps, or contains emoji that the terminal
    # measures differently from rich.
    console.set_alt_screen(True)
    console.show_cursor(False)
    try:
        while True:
            console.clear()
            console.print(build_interactive_layout(year, month, pal, db_path, status_msg=status_msg))

            try:
                key = readchar.readkey()
            except (KeyboardInterrupt, EOFError):
                break
            status_msg = ""

            if key in (readchar.key.LEFT, "h"):
                month -= 1
                if month < 1:
                    month, year = 12, year - 1
            elif key in (readchar.key.RIGHT, "l"):
                month += 1
                if month > 12:
                    month, year = 1, year + 1
            elif key in (readchar.key.UP, "k"):
                year += 1
            elif key in (readchar.key.DOWN, "j"):
                year -= 1
            elif key == "t":
                now = datetime.now()
                year, month = now.year, now.month
                status_msg = "Jumped to today!"
            elif key == "u":
                status_msg = speak_upcoming(db_path)
            elif key == "c":
                status_msg = speak_step(cycle, +1)
            elif key == "b":
                status_msg = speak_step(cycle, -1)
            elif key == "a":
                console.show_cursor(True)
                try:
                    status_msg, shown = add_event_flow(db_path, year, month, pal)
                finally:
                    console.show_cursor(False)
                if shown:
                    year, month = shown.year, shown.month
                cycle.reset()
            elif key == "x":
                console.show_cursor(True)
                try:
                    status_msg = delete_event_flow(db_path, year, month, pal)
                finally:
                    console.show_cursor(False)
                cycle.reset()
            elif key == "q":
                break
    finally:
        stop_speech()
        console.show_cursor(True)
        console.set_alt_screen(False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rich Terminal Calendar with Duplicate Protection")
    now = datetime.now()

    parser.add_argument(
        "-i", "--interactive", action="store_true",
        help="Launch interactive live view with month navigation and event editing",
    )
    parser.add_argument(
        "-y", "--year", type=int, nargs="?", const=now.year, default=None,
        help="Year to display",
    )
    parser.add_argument(
        "-m", "--month", type=int, choices=range(1, 13), metavar="1-12",
        nargs="?", const=now.month, default=None,
        help="Month to display",
    )
    parser.add_argument(
        "-d", "--day", type=int, choices=range(1, 32), metavar="1-31",
        nargs="?", const=now.day, default=None,
        help="Day to highlight",
    )
    parser.add_argument(
        "-t", "--theme", choices=THEMES, nargs="?", const="random", default="cyan",
        help=f"Color theme: {', '.join(THEMES)}",
    )
    parser.add_argument(
        "-e", "--events-file", type=Path, default=DEFAULT_DB_PATH, metavar="PATH",
        help="Path to JSON events file",
    )

    args = parser.parse_args()
    year = args.year or now.year
    month = args.month or (now.month if args.day or not args.year else None)

    if args.interactive:
        run_interactive_mode(
            start_year=year,
            start_month=month or now.month,
            theme=args.theme,
            db_path=args.events_file,
        )
        return

    # Standard static render
    if month is None and args.day is None and args.year is None:
        month = now.month

    events_map = get_events_map_for_period(args.events_file, year, month)
    pal = get_random_palette() if args.theme == "random" else PALETTES[args.theme]

    if month is not None:
        table = generate_month_table(
            year, month, highlight_day=args.day, palette=pal, show_title=False, today=now, events_map=events_map
        )
        console.print()
        console.print(Panel(table, title=f"[bold {pal.accent}]📅 {calendar.month_name[month]} {year}[/]", border_style=pal.accent, expand=False, padding=(1, 2)))
        console.print()
        panel = build_events_panel(events_map, palette=pal)
        if panel:
            console.print(panel)
            console.print()
    else:
        month_tables = [
            generate_month_table(year, m, palette=pal, show_title=True, today=now, events_map=events_map)
            for m in range(1, 13)
        ]
        console.print()
        console.print(Panel(Columns(month_tables, equal=True, expand=False, padding=(1, 2)), title=f"[bold {pal.accent}]✨ CALENDAR {year} ✨[/]", border_style=pal.accent, padding=(1, 2)))
        console.print()
        panel = build_events_panel(events_map, palette=pal)
        if panel:
            console.print(panel)
            console.print()


if __name__ == "__main__":
    main()