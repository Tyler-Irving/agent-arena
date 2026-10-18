from collections import defaultdict, deque


def rejected(requests: list[tuple[int, str]], limit: int, window: int) -> list[int]:
    accepted: dict[str, deque[int]] = defaultdict(deque)
    out = []
    for i, (t, user) in enumerate(requests):
        recent = accepted[user]
        while recent and recent[0] <= t - window:
            recent.popleft()
        if len(recent) < limit:
            recent.append(t)
        else:
            out.append(i)
    return out
