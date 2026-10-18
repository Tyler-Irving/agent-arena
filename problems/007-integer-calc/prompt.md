# Integer Calculator

Evaluate an arithmetic expression over integers and return the result.

The expression contains non-negative integer literals, the operators
`+ - * / % ^`, parentheses, and spaces (which can appear anywhere between
tokens and mean nothing).

Precedence, from loosest to tightest binding:

1. `+` and `-` (binary), left-associative
2. `*`, `/` and `%`, left-associative
3. unary `-` and unary `+`, which can be stacked: `--3` is `3`
4. `^` (exponentiation), **right-associative**: `2^3^2` is `2^(3^2)` = `512`

Because `^` binds tighter than unary minus, `-2^2` is `-(2^2)` = `-4`, and
`2*-3^2` is `2 * -(3^2)` = `-18`.

Division and remainder are **integer** operations that truncate toward zero,
like C or Java and **unlike** Python's `//` and `%`:

- `7 / 2` is `3` and `-7 / 2` is `-3`
- `7 % 2` is `1` and `-7 % 2` is `-1` (the remainder has the sign of the left operand)
- for any `a` and non-zero `b`: `(a / b) * b + a % b == a`

Dividing or taking the remainder by zero raises `ZeroDivisionError`. Exponents
are never negative, and `0^0` is `1`. Numbers and results can be arbitrarily
large integers, so don't go through floating point.

Expressions can be long (thousands of terms) and nested up to 100 parentheses
deep, so don't recurse once per operator.

## Signature

```python
def evaluate(expr: str) -> int:
```

## Examples

```python
evaluate("1 + 2 * 3")    # 7
evaluate("(1 + 2) * 3")  # 9
evaluate("2^3^2")        # 512
evaluate("-2^2")         # -4
evaluate("-7 / 2")       # -3
```
