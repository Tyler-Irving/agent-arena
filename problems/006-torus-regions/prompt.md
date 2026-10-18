# Wraparound Regions

A map is given as a list of equal-length strings. Each character is a cell:
a letter is land of that kind, and `.` is water.

A **region** is a group of cells with the same letter that are connected
through up, down, left and right steps between cells of that letter. Letters
are case-sensitive: `a` and `A` are different kinds. Water is never part of a
region.

The map wraps around **horizontally only**: the leftmost and rightmost cells of
each row are next to each other. The top and bottom rows are **not** next to
each other.

Return the number of regions. Maps can be large (hundreds of thousands of
cells), so the function must not rely on deep recursion.

## Signature

```python
def count_regions(grid: list[str]) -> int:
```

## Examples

```python
count_regions(["AAB", "BAA"])     # 3: the A cells, and two separate B cells
count_regions(["A.A"])            # 1: the two A cells touch across the edge
count_regions(["A", ".", "A"])    # 2: no wrap from bottom to top
```
