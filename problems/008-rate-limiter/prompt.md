# Rate Limiter

An API allows each user at most `limit` requests in any sliding window of
`window` seconds. Given a log of requests, work out which ones it rejects.

`requests` is a list of `(timestamp, user)` pairs, with timestamps as integer
seconds, sorted by timestamp. Requests with the same timestamp are handled in
list order.

A request from user `u` at time `t` is **accepted** if fewer than `limit` of
`u`'s **accepted** requests have timestamps in the window `(t - window, t]`,
that is, later than `t - window` and no later than `t`. Otherwise it is
**rejected**.

Things to get right:

- Rejected requests do not count toward the limit.
- A request exactly `window` seconds earlier is outside the window.
- Users are limited independently.
- `limit` can be `0`, which rejects everything.

Return the indexes (positions in `requests`) of the rejected requests, in
increasing order. Logs can hold hundreds of thousands of requests.

## Signature

```python
def rejected(requests: list[tuple[int, str]], limit: int, window: int) -> list[int]:
```

## Examples

```python
log = [(0, "a"), (1, "a"), (2, "a"), (10, "a")]
rejected(log, limit=2, window=10)   # [2]
# t=2 sees two accepted requests in (-8, 2]; t=10 sees only t=1, since t=0 is outside (0, 10]

log = [(0, "a"), (5, "a"), (9, "a"), (12, "a")]
rejected(log, limit=1, window=10)   # [1, 2]
# t=12 is accepted: the rejected requests at t=5 and t=9 don't count
```
