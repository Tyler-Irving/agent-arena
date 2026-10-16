def two_sum(nums: list[int], target: int) -> tuple[int, int]:
    seen: dict[int, int] = {}
    for j, n in enumerate(nums):
        if target - n in seen:
            return (seen[target - n], j)
        seen[n] = j
    raise ValueError("no pair")
