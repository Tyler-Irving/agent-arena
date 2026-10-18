from solution import expand


def test_simple_repeat():
    assert expand("3[ab]") == "ababab"


def test_nested():
    assert expand("2[a3[b]]") == "abbbabbb"


def test_plain_letters():
    assert expand("hello") == "hello"


def test_empty():
    assert expand("") == ""
    assert expand("4[]") == ""


def test_multi_digit_count():
    assert expand("12[x]") == "x" * 12


def test_zero_count():
    assert expand("a0[xyz]b") == "ab"
    assert expand("0[a2[b]]c") == "c"


def test_bracket_without_count_runs_once():
    assert expand("[ab]2[c]") == "abcc"
    assert expand("[[a]]") == "a"


def test_digits_not_before_bracket_are_text():
    assert expand("a3b") == "a3b"
    assert expand("10") == "10"
    assert expand("x42") == "x42"


def test_digits_then_letter_then_bracket():
    # "2" is followed by "a", not "[", so it is text; "[b]" has no count
    assert expand("2a[b]") == "2ab"


def test_text_digits_inside_brackets():
    assert expand("2[a3]") == "a3a3"
    assert expand("2[7]") == "77"


def test_count_right_after_text_digits():
    # the whole digit run "12" is the count, not "1" as text followed by "2[c]"
    assert expand("a12[c]") == "a" + "c" * 12


def test_siblings_and_suffix():
    assert expand("2[ab]3[c]d") == "ababcccd"


def test_deep_nesting():
    assert expand("3[3[3[3[3[ab]]]]]") == "ab" * 3**5
