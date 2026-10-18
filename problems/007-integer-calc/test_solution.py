import pytest
from solution import evaluate


def test_precedence():
    assert evaluate("1 + 2 * 3") == 7
    assert evaluate("(1 + 2) * 3") == 9
    assert evaluate("10 - 4 - 3") == 3


def test_single_number_and_spaces():
    assert evaluate("42") == 42
    assert evaluate("  1+  2 *3 ") == 7
    assert evaluate("( ( 5 ) )") == 5


def test_power_is_right_associative():
    assert evaluate("2^3^2") == 512
    assert evaluate("(2^3)^2") == 64


def test_power_binds_tighter_than_unary_minus():
    assert evaluate("-2^2") == -4
    assert evaluate("(-2)^2") == 4
    assert evaluate("2*-3^2") == -18


def test_stacked_unary_signs():
    assert evaluate("--3") == 3
    assert evaluate("-+-3") == 3
    assert evaluate("4 - -2") == 6
    assert evaluate("+5") == 5


def test_division_truncates_toward_zero():
    assert evaluate("7 / 2") == 3
    assert evaluate("-7 / 2") == -3
    assert evaluate("7 / -2") == -3
    assert evaluate("-7 / -2") == 3


def test_remainder_takes_sign_of_left_operand():
    assert evaluate("7 % 2") == 1
    assert evaluate("-7 % 2") == -1
    assert evaluate("7 % -2") == 1
    assert evaluate("-7 % -2") == -1


def test_division_identity():
    for a in (-17, -9, -1, 0, 5, 23):
        for b in (-5, -3, 1, 4, 7):
            q = evaluate(f"({a}) / ({b})")
            r = evaluate(f"({a}) % ({b})")
            assert q * b + r == a
            assert abs(r) < abs(b)


def test_big_integer_division_is_exact():
    # float division would lose digits here
    assert evaluate("(10^20 + 1) / 3") == (10**20 + 1) // 3
    assert evaluate("-(10^20 + 1) / 3") == -((10**20 + 1) // 3)
    assert evaluate("-(10^20 + 1) % 3") == -2  # Python's % would give 1
    assert evaluate("(2^80 + 5) % (2^40)") == 5


def test_left_associative_division():
    assert evaluate("100 / 10 / 5") == 2
    assert evaluate("100 % 7 * 2") == 4


def test_division_by_zero():
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / 0")
    with pytest.raises(ZeroDivisionError):
        evaluate("5 % (3 - 3)")


def test_big_integers():
    assert evaluate("2^100") == 2**100
    assert evaluate("3^4^2 - 1") == 3**16 - 1
    assert evaluate("123456789123456789 * 987654321987654321") == 123456789123456789 * 987654321987654321


def test_zero_exponent():
    assert evaluate("0^0") == 1
    assert evaluate("5^0 + 7") == 8


def test_mixed():
    assert evaluate("2 + 3 * (4 - 1) ^ 2 / 5 % 4") == 2 + (3 * 9 // 5) % 4
    assert evaluate("-(3 + 4) * 2 - -10 / 3") == -14 + 3


def test_deeply_nested_parentheses():
    depth = 100
    assert evaluate("(" * depth + "1 + 1" + ")" * depth) == 2


def test_long_flat_expression():
    expr = " + ".join(str(i) for i in range(1, 5001))
    assert evaluate(expr) == 5000 * 5001 // 2
