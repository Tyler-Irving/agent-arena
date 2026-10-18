import re


def evaluate(expr: str) -> int:
    tokens = re.findall(r"\d+|[-+*/%^()]", expr)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def additive() -> int:
        value = multiplicative()
        while peek() in ("+", "-"):
            op = take()
            right = multiplicative()
            value = value + right if op == "+" else value - right
        return value

    def multiplicative() -> int:
        value = unary()
        while peek() in ("*", "/", "%"):
            op = take()
            right = unary()
            if op == "*":
                value *= right
            else:
                if right == 0:
                    raise ZeroDivisionError("division by zero")
                quotient = abs(value) // abs(right)
                if (value < 0) != (right < 0):
                    quotient = -quotient
                value = quotient if op == "/" else value - quotient * right
        return value

    def unary() -> int:
        if peek() == "-":
            take()
            return -unary()
        if peek() == "+":
            take()
            return unary()
        return power()

    def power() -> int:
        base = atom()
        if peek() == "^":
            take()
            return base ** unary()  # right-associative; the exponent is never negative
        return base

    def atom() -> int:
        token = take()
        if token == "(":
            value = additive()
            take()  # ")"
            return value
        return int(token)

    return additive()
