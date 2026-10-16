# Two Sum

Given a list of integers `nums` and an integer `target`, return the indices of
the two numbers that add up to `target`, as a tuple `(i, j)` with `i < j`.

Exactly one valid pair exists. The same element may not be used twice, but the
same value may appear at two different indices.

## Signature

```python
def two_sum(nums: list[int], target: int) -> tuple[int, int]:
```

## Examples

```python
two_sum([2, 7, 11, 15], 9)  # (0, 1)
two_sum([3, 2, 4], 6)       # (1, 2)
```
