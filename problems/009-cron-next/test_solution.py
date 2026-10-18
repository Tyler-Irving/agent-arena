import time
from datetime import datetime

from solution import next_run


def test_every_fifteen_minutes():
    assert next_run("*/15 * * * *", datetime(2026, 3, 1, 10, 7)) == datetime(2026, 3, 1, 10, 15)


def test_strictly_later():
    assert next_run("*/15 * * * *", datetime(2026, 3, 1, 10, 15)) == datetime(2026, 3, 1, 10, 30)
    assert next_run("*/15 * * * *", datetime(2026, 3, 1, 10, 15, 30)) == datetime(2026, 3, 1, 10, 30)
    assert next_run("*/15 * * * *", datetime(2026, 3, 1, 10, 14, 59, 999)) == datetime(2026, 3, 1, 10, 15)


def test_weekdays_skip_the_weekend():
    # 2026-03-06 is a Friday
    assert next_run("0 9 * * 1-5", datetime(2026, 3, 6, 9, 0)) == datetime(2026, 3, 9, 9, 0)


def test_rolls_over_hour_day_and_year():
    assert next_run("5 * * * *", datetime(2026, 3, 1, 23, 59)) == datetime(2026, 3, 2, 0, 5)
    assert next_run("0 0 1 1 *", datetime(2026, 12, 31, 23, 59)) == datetime(2027, 1, 1, 0, 0)


def test_lists_ranges_and_steps():
    assert next_run("0,30 9,17 * * *", datetime(2026, 3, 1, 9, 30)) == datetime(2026, 3, 1, 17, 0)
    assert next_run("10-20/5 8-10 * * *", datetime(2026, 3, 1, 10, 21)) == datetime(2026, 3, 2, 8, 10)
    assert next_run("1-10/4 0 * * *", datetime(2026, 3, 1, 0, 5)) == datetime(2026, 3, 1, 0, 9)


def test_day_of_month_step_starts_at_one():
    # */10 in the day field is 1, 11, 21, 31
    assert next_run("0 0 */10 * *", datetime(2026, 3, 2)) == datetime(2026, 3, 11)
    assert next_run("0 0 */10 2 *", datetime(2026, 2, 21)) == datetime(2027, 2, 1)


def test_short_months_are_skipped():
    assert next_run("0 12 31 * *", datetime(2026, 4, 1)) == datetime(2026, 5, 31, 12, 0)


def test_sunday_is_zero_and_seven():
    # 2026-03-01 is a Sunday
    expected = datetime(2026, 3, 8, 0, 0)
    assert next_run("0 0 * * 0", datetime(2026, 3, 1, 0, 0)) == expected
    assert next_run("0 0 * * 7", datetime(2026, 3, 1, 0, 0)) == expected
    assert next_run("0 0 * * 5-7", datetime(2026, 3, 1, 0, 0)) == datetime(2026, 3, 6, 0, 0)


def test_day_of_month_or_day_of_week():
    # both restricted: the 13th OR any Friday
    assert next_run("0 0 13 * 5", datetime(2026, 3, 1)) == datetime(2026, 3, 6)  # Friday the 6th
    assert next_run("0 0 13 * 5", datetime(2026, 3, 13)) == datetime(2026, 3, 20)  # next Friday
    assert next_run("0 0 13 * 5", datetime(2026, 4, 10)) == datetime(2026, 4, 13)  # Monday the 13th


def test_only_the_restricted_day_field_decides():
    # day of week is *, so only the 13th counts, Fridays don't
    assert next_run("0 0 13 * *", datetime(2026, 3, 1)) == datetime(2026, 3, 13)
    # day of month is *, so only Fridays count
    assert next_run("0 0 * * 5", datetime(2026, 3, 7)) == datetime(2026, 3, 13)


def test_star_slash_one_counts_as_restricted():
    # */1 matches every day, but it is not exactly *, so the OR rule applies: every day matches
    assert next_run("0 0 */1 * 1", datetime(2026, 3, 2)) == datetime(2026, 3, 3)
    # with a plain * only Mondays match
    assert next_run("0 0 * * 1", datetime(2026, 3, 2)) == datetime(2026, 3, 9)


def test_february_twenty_ninth():
    assert next_run("30 6 29 2 *", datetime(2026, 1, 1)) == datetime(2028, 2, 29, 6, 30)
    assert next_run("30 6 29 2 *", datetime(2028, 2, 29, 6, 30)) == datetime(2032, 2, 29, 6, 30)


def test_month_and_weekday_with_or_rule():
    # in February only: the 29th OR any Monday; 2027-02-01 is a Monday
    assert next_run("0 0 29 2 1", datetime(2026, 3, 1)) == datetime(2027, 2, 1)


def test_never_matches():
    assert next_run("0 0 30 2 *", datetime(2026, 1, 1)) is None
    assert next_run("0 0 31 4,6,9,11 *", datetime(2026, 1, 1)) is None


def test_search_reaches_the_end_of_year_plus_eight():
    assert next_run("59 23 31 12 *", datetime(2026, 12, 31, 23, 59)) == datetime(2027, 12, 31, 23, 59)
    assert next_run("0 0 29 2 *", datetime(2032, 3, 1)) == datetime(2036, 2, 29)
    # 2100 is not a leap year, so the next 29 February after 2096 is in 2104:
    # just inside the limit of 31 Dec 2104, but more than 8 * 365 days away
    assert next_run("0 0 29 2 *", datetime(2096, 3, 1)) == datetime(2104, 2, 29)


def test_rare_schedules_are_fast():
    start = time.perf_counter()
    assert next_run("0 0 30 2 *", datetime(2026, 1, 1)) is None
    assert next_run("0 0 31 2,4 *", datetime(2026, 1, 1)) is None
    assert next_run("59 23 29 2 *", datetime(2028, 3, 1)) == datetime(2032, 2, 29, 23, 59)
    assert time.perf_counter() - start < 2.0
