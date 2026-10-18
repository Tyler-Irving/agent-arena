from solution import sort_versions


def test_numeric_not_lexical():
    assert sort_versions(["1.10", "1.9", "1.2.3"]) == ["1.2.3", "1.9", "1.10"]


def test_already_sorted_and_empty():
    assert sort_versions([]) == []
    assert sort_versions(["0.1", "0.2", "1.0"]) == ["0.1", "0.2", "1.0"]


def test_prerelease_below_release():
    assert sort_versions(["1.0.0", "1.0.0-rc.1"]) == ["1.0.0-rc.1", "1.0.0"]


def test_prerelease_above_lower_release():
    assert sort_versions(["1.0.0-alpha", "0.9.9"]) == ["0.9.9", "1.0.0-alpha"]


def test_missing_parts_are_zero_and_ties_keep_order():
    assert sort_versions(["v1.2", "1.2.0", "1.2"]) == ["v1.2", "1.2.0", "1.2"]
    assert sort_versions(["1.2.0.0", "1.1.9", "1.2"]) == ["1.1.9", "1.2.0.0", "1.2"]


def test_leading_v_ignored_either_case():
    assert sort_versions(["V2.0", "v1.5", "1.7"]) == ["v1.5", "1.7", "V2.0"]


def test_leading_zeros_compare_as_numbers():
    assert sort_versions(["1.02", "1.1", "1.10"]) == ["1.1", "1.02", "1.10"]


def test_full_prerelease_ladder():
    ladder = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    shuffled = [ladder[i] for i in (5, 0, 7, 2, 6, 1, 4, 3)]
    assert sort_versions(shuffled) == ladder


def test_numeric_identifier_below_alphanumeric():
    assert sort_versions(["1.0-x", "1.0-99"]) == ["1.0-99", "1.0-x"]


def test_prerelease_with_missing_release_parts_ties():
    assert sort_versions(["2.0.0-rc", "2.0-rc"]) == ["2.0.0-rc", "2.0-rc"]


def test_alphanumeric_identifiers_use_ascii_order():
    # uppercase letters sort before lowercase in ASCII
    assert sort_versions(["1.0-beta", "1.0-Beta", "1.0-alpha"]) == ["1.0-Beta", "1.0-alpha", "1.0-beta"]


def test_returns_original_strings():
    original = ["v3.0.0", "V1.0", "2.0.0-RC.1"]
    assert sort_versions(original) == ["V1.0", "2.0.0-RC.1", "v3.0.0"]
