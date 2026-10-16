# Merge Intervals

You are given a list of closed integer intervals `(start, end)` with
`start <= end`, in no particular order. Merge every group of intervals that
overlap or touch, and return the merged intervals sorted by start.

Two intervals touch when one ends exactly where the other starts: `(1, 4)` and
`(4, 7)` merge into `(1, 7)`. Intervals that are merely adjacent integers do
**not** merge: `(1, 2)` and `(3, 4)` stay separate.

Return a list of `(start, end)` tuples. An empty input returns an empty list.

## Signature

```python
def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
```

## Examples

```python
merge_intervals([(1, 3), (2, 6), (8, 10), (15, 18)])  # [(1, 6), (8, 10), (15, 18)]
merge_intervals([(5, 7), (1, 5)])                      # [(1, 7)]
merge_intervals([(1, 2), (3, 4)])                      # [(1, 2), (3, 4)]
```
