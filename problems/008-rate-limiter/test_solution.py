from bisect import bisect_right
from collections import defaultdict

from solution import rejected


def test_example_window_boundary():
    log = [(0, "a"), (1, "a"), (2, "a"), (10, "a")]
    assert rejected(log, limit=2, window=10) == [2]


def test_rejected_requests_do_not_count():
    log = [(0, "a"), (5, "a"), (9, "a"), (12, "a")]
    assert rejected(log, limit=1, window=10) == [1, 2]


def test_empty_log():
    assert rejected([], limit=3, window=5) == []


def test_limit_zero_rejects_everything():
    log = [(0, "a"), (100, "b"), (200, "a")]
    assert rejected(log, limit=0, window=1) == [0, 1, 2]


def test_users_are_independent():
    log = [(0, "a"), (0, "b"), (1, "a"), (1, "b"), (2, "a")]
    assert rejected(log, limit=1, window=5) == [2, 3, 4]


def test_same_timestamp_in_list_order():
    log = [(7, "a"), (7, "a"), (7, "a"), (7, "b")]
    assert rejected(log, limit=2, window=1) == [2]


def test_window_of_one_second():
    # the window (t-1, t] holds only requests at exactly t
    log = [(0, "a"), (0, "a"), (1, "a"), (1, "a"), (1, "a")]
    assert rejected(log, limit=2, window=1) == [4]


def test_exactly_window_apart_is_outside():
    # limit 1 per 30 s: 30, 60 and 90 are each exactly one window after the
    # previous accepted request, so they are accepted; 89 is not
    log = [(0, "a"), (30, "a"), (60, "a"), (89, "a"), (90, "a")]
    assert rejected(log, limit=1, window=30) == [3]


def test_burst_then_recovery():
    log = [(t, "a") for t in (0, 0, 0, 0, 3, 5, 6, 9, 10, 10)]
    # limit 3 per 5 s:
    #   t=0,0,0 accepted; the 4th t=0 and t=3 rejected (three accepted in the window)
    #   t=5 accepted (the t=0 requests are out of (0, 5]), then t=6, t=9 and the first t=10
    #   the second t=10 sees 6, 9 and 10 in (5, 10] and is rejected
    assert rejected(log, limit=3, window=5) == [3, 4, 9]


def test_many_users_interleaved():
    # user k sends at k, k+7, k+14, ...
    log = [(t, f"user{t % 7}") for t in range(700)]
    # two requests per 10 s are allowed and at most two ever fall in a window
    assert rejected(log, limit=2, window=10) == []
    # one per 10 s: every second request of each user comes 7 s after an accepted one
    assert rejected(log, limit=1, window=10) == [i for i in range(700) if (i // 7) % 2 == 1]


def test_large_log_single_user():
    n = 100_000
    log = [(i, "u") for i in range(n)]
    assert rejected(log, limit=1000, window=1_000_000_000) == list(range(1000, n))


def test_large_log_sliding():
    n = 200_000
    log = [(i // 4, f"u{i % 3}") for i in range(n)]
    # independent check: binary search over each user's accepted timestamps
    accepted: dict[str, list[int]] = defaultdict(list)
    expected = []
    for i, (t, user) in enumerate(log):
        times = accepted[user]
        if len(times) - bisect_right(times, t - 60) < 50:
            times.append(t)
        else:
            expected.append(i)
    assert rejected(log, limit=50, window=60) == expected
