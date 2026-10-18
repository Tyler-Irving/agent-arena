from datetime import date, datetime, timedelta

RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def parse_field(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for item in field.split(","):
        base, _, step_text = item.partition("/")
        step = int(step_text) if step_text else 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-")
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        values.update(range(start, end + 1, step))
    return values


def next_run(schedule: str, after: datetime) -> datetime | None:
    fields = schedule.split()
    minutes, hours, days, months, weekdays = (
        parse_field(f, low, high) for f, (low, high) in zip(fields, RANGES, strict=True)
    )
    weekdays = {0 if d == 7 else d for d in weekdays}
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    sorted_hours = sorted(hours)
    sorted_minutes = sorted(minutes)

    def day_matches(day: date) -> bool:
        if day.month not in months:
            return False
        dom = day.day in days
        dow = (day.weekday() + 1) % 7 in weekdays  # Python: Monday=0; cron: Sunday=0
        if dom_restricted and dow_restricted:
            return dom or dow
        if dom_restricted:
            return dom
        if dow_restricted:
            return dow
        return True

    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    last = date(after.year + 8, 12, 31)
    first_day = True
    while day <= last:
        if day_matches(day):
            for hour in sorted_hours:
                if first_day and hour < start.hour:
                    continue
                for minute in sorted_minutes:
                    if first_day and hour == start.hour and minute < start.minute:
                        continue
                    return datetime(day.year, day.month, day.day, hour, minute)
        day += timedelta(days=1)
        first_day = False
    return None
