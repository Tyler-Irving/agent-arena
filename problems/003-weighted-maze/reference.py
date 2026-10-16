import heapq


def cheapest_path(maze: list[str]) -> int:
    rows, cols = len(maze), len(maze[0])
    start = next((r, c) for r in range(rows) for c in range(cols) if maze[r][c] == "S")
    best = {start: 0}
    queue = [(0, start)]
    while queue:
        cost, (r, c) = heapq.heappop(queue)
        if maze[r][c] == "E":
            return cost
        if cost > best[(r, c)]:
            continue
        for nr, nc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
            if not (0 <= nr < rows and 0 <= nc < cols) or maze[nr][nc] == "#":
                continue
            cell = maze[nr][nc]
            step = int(cell) if cell.isdigit() else 1
            if cost + step < best.get((nr, nc), float("inf")):
                best[(nr, nc)] = cost + step
                heapq.heappush(queue, (cost + step, (nr, nc)))
    return -1
