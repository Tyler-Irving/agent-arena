from solution import two_sum


def test_example():
    assert tuple(two_sum([2, 7, 11, 15], 9)) == (0, 1)


def test_does_not_reuse_element():
    assert tuple(two_sum([3, 2, 4], 6)) == (1, 2)


def test_duplicate_values():
    assert tuple(two_sum([3, 3], 6)) == (0, 1)


def test_negatives():
    assert tuple(two_sum([-1, -2, -3, -4, -5], -8)) == (2, 4)


def test_zeros():
    assert tuple(two_sum([0, 4, 3, 0], 0)) == (0, 3)


def test_pair_at_end_of_long_list():
    assert tuple(two_sum(list(range(10_000)), 19_997)) == (9998, 9999)
