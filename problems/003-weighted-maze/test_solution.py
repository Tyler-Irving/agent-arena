from solution import cheapest_path


def test_straight_line():
    assert cheapest_path(["S.E"]) == 2


def test_blocked():
    assert cheapest_path(["S#E"]) == -1


def test_detour_is_cheaper():
    assert cheapest_path(["S9E", "..."]) == 4


def test_vertical():
    assert cheapest_path(["S", "E"]) == 1


def test_forced_through_rough_ground():
    assert cheapest_path(["S2", "#3", "E1"]) == 7


def test_walled_in():
    maze = [
        ".....",
        ".###.",
        ".#S#.",
        ".###.",
        "....E",
    ]
    assert cheapest_path(maze) == -1


def test_shortest_by_steps_is_not_cheapest():
    maze = [
        "S5555E",
        "1####1",
        "111111",
    ]
    # straight across: 5*4 + 1 = 21; around the bottom: 9 moves, 8 onto 1s + 1 onto E = 9
    assert cheapest_path(maze) == 9


def test_large_uniform_grid():
    n = 300
    rows = ["5" * n for _ in range(n)]
    rows[0] = "S" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "E"
    # 2*(n-1) moves: every move costs 5 except the last one onto E
    assert cheapest_path(rows) == 5 * (2 * (n - 1) - 1) + 1
