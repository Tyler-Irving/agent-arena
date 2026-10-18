from solution import count_regions


def test_example():
    assert count_regions(["AAB", "BAA"]) == 3


def test_wraps_left_to_right():
    assert count_regions(["A.A"]) == 1
    assert count_regions(["A..", "..A"]) == 2


def test_no_wrap_top_to_bottom():
    assert count_regions(["A", ".", "A"]) == 2
    assert count_regions(["A.", "..", "A."]) == 2


def test_empty_and_all_water():
    assert count_regions([]) == 0
    assert count_regions(["...", "..."]) == 0


def test_case_sensitive():
    assert count_regions(["aA"]) == 2


def test_single_column_and_single_cell():
    assert count_regions(["A"]) == 1
    assert count_regions(["A", "A", "B", "A"]) == 3


def test_diagonals_do_not_connect():
    assert count_regions(["A.", ".A"]) == 2


def test_region_joined_only_through_the_wrap():
    grid = [
        "B.B",
        "B.B",
        "...",
    ]
    assert count_regions(grid) == 1


def test_stripes():
    grid = ["ABAB" * 25 for _ in range(10)]
    # every column is its own region; with an even width the wrap never joins two columns
    assert count_regions(grid) == 100


def test_rows_joined_across_the_edge():
    grid = [
        "CC.CC",
        "..D..",
        "CC.CC",
    ]
    # without the wrap this would be 5 regions
    assert count_regions(grid) == 3


def test_large_single_region():
    grid = ["A" * 600 for _ in range(600)]
    assert count_regions(grid) == 1


def test_large_snake():
    # one long winding path: rows of A joined at alternating ends. The last
    # column is water, so the wrap can't join the two ends of a row and the
    # path really is ~80,000 cells long
    width, height = 401, 401
    grid = []
    for r in range(height):
        if r % 2 == 0:
            row = "A" * (width - 1)
        elif r % 4 == 1:
            row = "." * (width - 2) + "A"
        else:
            row = "A" + "." * (width - 2)
        grid.append(row + ".")
    assert count_regions(grid) == 1


def test_large_checkerboard():
    width, height = 500, 400
    grid = ["".join("XO"[(r + c) % 2] for c in range(width)) for r in range(height)]
    assert count_regions(grid) == width * height
