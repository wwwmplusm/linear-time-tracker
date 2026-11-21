from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from tracker.db import Row, elapsed_ms


Period = Literal["day", "week", "month"]
PERIODS = {"day": "День", "week": "Неделя", "month": "Месяц"}
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DAY = timedelta(days=1)
FIRST_DATE = date(2, 1, 1)


def period_bounds(period: Period, anchor: date) -> tuple[date, date]:
    if period == "month":
        start = anchor.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + 7 * DAY
    return anchor, anchor + DAY


def statistics_url(period: Period, anchor: date) -> str:
    return f"/statistics?period={period}&on={anchor.isoformat()}"


def midnight_ms(day: date, zone: ZoneInfo) -> int:
    return int(datetime.combine(day, time.min, zone).timestamp()) * 1000


def day_label(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def period_label(start: date, end: date) -> str:
    last = end - DAY
    if start == last:
        return f"{day_label(start)} {start.year}"
    if start.month == last.month and start.year == last.year:
        return f"{start.day}–{day_label(last)} {last.year}"
    first = day_label(start) if start.year == last.year else f"{day_label(start)} {start.year}"
    return f"{first} — {day_label(last)} {last.year}"


def duration_label(ms: int) -> str:
    seconds = ms // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин"
    if 0 < ms < 1000:
        return "<1 с"
    return f"{seconds} с" if seconds else "0 мин"


def report_data(
    period: Period, start: date, end: date, today: date, rows: list[Row], zone: ZoneInfo,
) -> dict:
    buckets = Counter()
    tasks = Counter()
    journal = {}
    total = previous_total = count = previous_count = 0

    for row in rows:
        finished = datetime.fromtimestamp(row["ended_ms"] / 1000, zone)
        day = finished.date()
        duration = elapsed_ms(row, row["ended_ms"])
        if day < start:
            previous_total += duration
            previous_count += 1
            continue
        total += duration
        count += 1
        tasks[row["target"]] += duration
        buckets[finished.hour if period == "day" else day] += duration
        journal.setdefault(day, []).append((row, duration))

    if period == "day":
        bars = [
            (f"{hour:02d}:00", f"{hour:02d}" if hour % 3 == 0 else "", buckets[hour])
            for hour in range(24)
        ]
    else:
        days = [start + index * DAY for index in range((end - start).days)]
        bars = [
            (day_label(day),
             WEEKDAYS[day.weekday()] if period == "week" else str(day.day) if day.day % 5 == 1 else "",
             buckets[day])
            for day in days
        ]

    return {
        "total": total,
        "count": count,
        "duration_delta": total - previous_total,
        "count_delta": count - previous_count,
        "daily_average": total // (min(end, today + DAY) - start).days,
        "tasks": tasks.most_common(),
        "journal": journal.items(),
        "bars": bars,
        "chart_max": max(1, max(value for _, _, value in bars)),
    }
