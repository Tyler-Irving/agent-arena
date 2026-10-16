# Weighted Maze

A maze is given as a list of equal-length strings. Each character is a cell:

- `S` start (exactly one)
- `E` end (exactly one)
- `#` wall, cannot be entered
- `.` open floor
- `1`-`9` rough ground

You move one cell at a time up, down, left or right, never diagonally and never
off the grid. Entering a cell costs:

- `9`..`1` rough ground: its digit value
- `.`, `S` or `E`: 1

Standing on the start costs nothing. Return the minimum total cost to reach `E`
from `S`, or `-1` if `E` cannot be reached.

## Signature

```python
def cheapest_path(maze: list[str]) -> int:
```

## Examples

```python
cheapest_path(["S.E"])           # 2
cheapest_path(["S#E"])           # -1
cheapest_path(["S9E",
               "..."])           # 4: going around the 9 is cheaper
```
