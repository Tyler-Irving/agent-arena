def count_regions(grid: list[str]) -> int:
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    seen = [[False] * cols for _ in range(rows)]
    regions = 0
    for r in range(rows):
        for c in range(cols):
            kind = grid[r][c]
            if kind == "." or seen[r][c]:
                continue
            regions += 1
            seen[r][c] = True
            stack = [(r, c)]
            while stack:
                cr, cc = stack.pop()
                for nr, nc in ((cr + 1, cc), (cr - 1, cc), (cr, (cc + 1) % cols), (cr, (cc - 1) % cols)):
                    if 0 <= nr < rows and not seen[nr][nc] and grid[nr][nc] == kind:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
    return regions
