from collections import defaultdict


def settle(debts: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    balance: dict[str, int] = defaultdict(int)  # positive: is owed money
    for debtor, creditor, amount in debts:
        balance[debtor] -= amount
        balance[creditor] += amount
    people = [p for p in sorted(balance) if balance[p] != 0]
    n = len(people)

    # Splitting the people into k groups that each sum to zero needs n - k
    # transfers (each group of size s settles in s - 1), and no answer can do
    # better. best[mask] is the most zero-sum groups the people in mask can be
    # split into; a group closes each time the running total returns to zero.
    total = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = (mask & -mask).bit_length() - 1
        total[mask] = total[mask & (mask - 1)] + balance[people[low]]
    best = [0] * (1 << n)
    came_from = [0] * (1 << n)
    for mask in range(1, 1 << n):
        for i in range(n):
            if mask >> i & 1 and best[mask ^ (1 << i)] >= best[mask]:
                best[mask] = best[mask ^ (1 << i)]
                came_from[mask] = i
        if total[mask] == 0:
            best[mask] += 1

    # Recover the order people were added in, then cut it into zero-sum groups.
    order = []
    mask = (1 << n) - 1
    while mask:
        i = came_from[mask]
        order.append(i)
        mask ^= 1 << i
    order.reverse()

    transfers = []
    group: list[str] = []
    running = 0
    for i in order:
        group.append(people[i])
        running += balance[people[i]]
        if running == 0:
            transfers += settle_group(group, balance)
            group = []
    return transfers


def settle_group(group: list[str], balance: dict[str, int]) -> list[tuple[str, str, int]]:
    """Settle a zero-sum group in len(group) - 1 transfers or fewer."""
    owe = [[p, -balance[p]] for p in group if balance[p] < 0]
    owed = [[p, balance[p]] for p in group if balance[p] > 0]
    out = []
    i = j = 0
    while i < len(owe) and j < len(owed):
        amount = min(owe[i][1], owed[j][1])
        out.append((owe[i][0], owed[j][0], amount))
        owe[i][1] -= amount
        owed[j][1] -= amount
        if owe[i][1] == 0:
            i += 1
        if owed[j][1] == 0:
            j += 1
    return out
