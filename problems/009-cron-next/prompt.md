# Next Cron Run

Given a cron schedule and a moment in time, return the next time the schedule
fires.

A schedule has five fields separated by single spaces:

| Field        | Values | Notes                              |
| ------------ | ------ | ---------------------------------- |
| minute       | 0-59   |                                    |
| hour         | 0-23   |                                    |
| day of month | 1-31   |                                    |
| month        | 1-12   |                                    |
| day of week  | 0-7    | 0 and 7 both mean Sunday, 1 is Monday |

Each field is a comma-separated list of one or more items. An item is:

- `*`, every value of the field
- a number, like `5`
- a range `a-b`, inclusive, with `a <= b`
- a step `*/n` or `a-b/n`, every `n`th value starting from the first value of
  the `*` or range: `*/15` in the minute field is `0,15,30,45`, `*/10` in the
  day-of-month field is `1,11,21,31`, and `1-10/4` is `1,5,9`

**Day matching** follows classic cron: if both the day-of-month and the
day-of-week fields are restricted (anything other than exactly `*`), a day
matches when **either** field matches. If one of them is exactly `*`, only the
other one decides. For example `0 0 13 * 5` fires at midnight on every 13th of
the month **and** on every Friday.

Return the first minute that is **strictly later** than `after` and matches
every field, as a `datetime` with seconds and microseconds set to 0. Any
seconds or microseconds in `after` are ignored except that the answer must
still be later than `after`. Times are naive local times with no time zones or
daylight saving.

Search up to and including 31 December of year `after.year + 8`. If nothing
matches in that time (for example `0 0 30 2 *`, the 30th of February), return
`None`. Some schedules match rarely or never, so the function must answer in
well under a second; checking every minute one by one is too slow.

All schedules in the tests are valid.

## Signature

```python
from datetime import datetime

def next_run(schedule: str, after: datetime) -> datetime | None:
```

## Examples

```python
next_run("*/15 * * * *", datetime(2026, 3, 1, 10, 7))     # datetime(2026, 3, 1, 10, 15)
next_run("0 9 * * 1-5", datetime(2026, 3, 6, 9, 0))       # datetime(2026, 3, 9, 9, 0): Fri 9:00 has passed, next is Mon
next_run("30 6 29 2 *", datetime(2026, 1, 1))             # datetime(2028, 2, 29, 6, 30)
next_run("0 0 30 2 *", datetime(2026, 1, 1))              # None
```
