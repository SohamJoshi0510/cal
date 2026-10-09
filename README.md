# cal
A python program that serves as an interactive command line calendar.

# Terminal Calendar

A keyboard-driven terminal calendar built with Python and Rich. View monthly schedules, add and delete events, configure recurring events, and hear upcoming reminders through text-to-speech.

## Features

- Monthly and yearly calendar views with highlighted dates and event markers.
- Interactive navigation between months and years.
- Add events with flexible date input, including dates such as `today`, `tomorrow`, weekday names, and numeric date formats.
- Recurrence options: once, weekly, monthly, yearly, selected weekdays, specific dates, or a daily date range.
- Duplicate-event warnings and confirmation before adding duplicates.
- Review an event's details before saving.
- Delete a single event or an occurrence from a recurring series, or remove the entire series.
- Browse upcoming events and cycle through them with optional spoken announcements.
- Select a color theme, including a random theme.
- Store events in a local `events.json` file.

## Requirements

- Python 3.10 or newer
- `rich`
- `readchar` (required for interactive mode)
- `pyttsx3` (optional; enables spoken announcements)

## Installation

```bash
python -m pip install rich readchar pyttsx3
```

`pyttsx3` is optional if you do not need text-to-speech.

## Usage

Run these commands from the directory containing `cal.py`:

```bash
# Show the current month's calendar
python cal.py

# Start interactive mode
python cal.py --interactive

# Display a specific month and year
python cal.py --year 2026 --month 10

# Display an entire year's calendar
python cal.py --year 2026

# Use a different color theme
python cal.py --theme blue

# Use a specific events database
python cal.py --events-file ./my_events.json
```

### Interactive keyboard controls

| Key | Action |
|---|---|
| `h` / `Left Arrow` | Previous month |
| `l` / `Right Arrow` | Next month |
| `j` / `Down Arrow` | Previous year |
| `k` / `Up Arrow` | Next year |
| `t` | Jump to today |
| `a` | Add an event |
| `x` | Delete an event |
| `u` | Announce upcoming events |
| `c` | Speak the next event in the cycle |
| `b` | Speak the previous event in the cycle |
| `q` | Quit interactive mode |

## Adding events

The add flow displays the calendar and scheduled events for context, accepts flexible date input, lets you configure recurrence, and presents a summary for confirmation before saving. Type `q` at a prompt to cancel.

Examples of date input include:

- `today` or `tomorrow`
- Weekday names or abbreviations, such as `sun` or `wed`
- Dates such as `15`, `15/3`, `15/3/2027`, or `2026-10-15`

For weekdays, the parser chooses the next matching weekday, including today if it is already that weekday.

## Data storage

Events are stored locally in `events.json` by default, in the same directory as the script. The file is created when needed. You can choose another location with `--events-file`. Keep a backup of this file if your event list is important.

## Notes

- Interactive mode requires a real terminal and the `readchar` package.
- Spoken announcements require `pyttsx3` and a working system text-to-speech engine.
- Use `python cal.py --help` to see all command-line options.
