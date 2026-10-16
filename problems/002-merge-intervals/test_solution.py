from solution import merge_intervals


def merged(intervals):
    return [tuple(iv) for iv in merge_intervals(intervals)]


def test_example():
    assert merged([(1, 3), (2, 6), (8, 10), (15, 18)]) == [(1, 6), (8, 10), (15, 18)]


def test_empty():
    assert merged([]) == []


def test_single():
    assert merged([(4, 4)]) == [(4, 4)]


def test_unsorted_input():
    assert merged([(8, 10), (1, 3), (2, 6)]) == [(1, 6), (8, 10)]


def test_touching_endpoints_merge():
    assert merged([(5, 7), (1, 5)]) == [(1, 7)]


def test_adjacent_integers_do_not_merge():
    assert merged([(1, 2), (3, 4)]) == [(1, 2), (3, 4)]


def test_contained_interval():
    assert merged([(1, 10), (2, 3), (4, 12)]) == [(1, 12)]


def test_chain_merges_through_later_interval():
    # (1, 2) and (3, 4) only join once (2, 3) is considered
    assert merged([(1, 2), (3, 4), (2, 3)]) == [(1, 4)]


def test_many_intervals():
    intervals = [(i, i + 1) for i in range(0, 20_000, 2)] + [(i + 1, i + 2) for i in range(0, 20_000, 2)]
    assert merged(intervals) == [(0, 20_000)]
