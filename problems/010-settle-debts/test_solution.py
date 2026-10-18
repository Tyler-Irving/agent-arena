from collections import defaultdict

from solution import settle


def net(debts):
    balance = defaultdict(int)
    for debtor, creditor, amount in debts:
        balance[debtor] -= amount
        balance[creditor] += amount
    return {p: v for p, v in balance.items() if v != 0}


def through_hub(balances):
    """Records that produce these balances, all routed through "hub", who ends up even."""
    return [(p, "hub", -v) if v < 0 else ("hub", p, v) for p, v in balances.items()]


def check(debts, fewest):
    transfers = settle(debts)
    balance = net(debts)
    assert len(transfers) == fewest
    remaining = dict(balance)
    for payer, payee, amount in transfers:
        assert type(amount) is int and amount > 0
        assert payer != payee
        assert payer in balance and payee in balance, "only people with a non-zero balance transfer"
        remaining[payer] += amount
        remaining[payee] -= amount
    assert all(v == 0 for v in remaining.values())


def test_single_debt():
    assert settle([("ann", "bob", 500)]) == [("ann", "bob", 500)]


def test_middle_person_drops_out():
    assert settle([("ann", "bob", 500), ("bob", "cat", 500)]) == [("ann", "cat", 500)]


def test_cancelling_debts():
    assert settle([("ann", "bob", 300), ("bob", "ann", 300)]) == []


def test_empty_and_self_debt():
    assert settle([]) == []
    assert settle([("ann", "ann", 100)]) == []


def test_repeated_pairs_are_combined():
    debts = [("a", "b", 100), ("a", "b", 50), ("b", "a", 30)]
    assert settle(debts) == [("a", "b", 120)]


def test_long_chain():
    debts = [("a", "b", 70), ("b", "c", 70), ("c", "d", 70), ("d", "e", 70)]
    assert settle(debts) == [("a", "e", 70)]


def test_one_creditor_many_debtors():
    debts = [("a", "z", 10), ("b", "z", 20), ("c", "z", 30)]
    check(debts, 3)


def test_even_hub_never_appears():
    check(through_hub({"ann": -250, "bob": 250}), 1)


def test_two_independent_pairs():
    check(through_hub({"a": -40, "b": 40, "c": -75, "d": 75}), 2)


def test_largest_to_largest_is_not_fewest():
    # greedy starts with fay (largest debtor) paying bea (largest creditor) and
    # needs 4 transfers; but {dee, bea} and {fay, ada, eve} each settle on their
    # own, so 1 + 2 = 3 is enough
    check(through_hub({"fay": -13, "dee": -9, "bea": 9, "ada": 7, "eve": 6}), 3)


def test_twelve_people_in_four_hidden_groups():
    balances = {
        "amy": -474, "ben": -685, "cal": 323, "dee": 319, "eli": 431, "fay": 670,
        "gus": -174, "hal": 619, "ivy": -149, "jon": 254, "kim": -938, "lou": -196,
    }  # fmt: skip
    # four groups of three that each sum to zero: 4 * 2 = 8 transfers
    # (largest-debtor-pays-largest-creditor needs 11)
    check(through_hub(balances), 8)


def test_twelve_people_no_shortcut():
    # no smaller group sums to zero, so it takes n - 1 = 11 transfers
    names = ["amy", "ben", "cal", "dee", "eli", "fay", "gus", "hal", "ivy", "jon", "kim"]
    balances = {name: -(2**i) for i, name in enumerate(names)}
    balances["lou"] = 2**11 - 1
    check(through_hub(balances), 11)


def test_mixed_records():
    debts = [
        ("ann", "bob", 1200), ("bob", "cat", 450), ("cat", "ann", 300),
        ("dan", "ann", 800), ("bob", "dan", 800), ("cat", "cat", 99),
    ]  # fmt: skip
    # nets: ann -100, bob -50, cat +150, and dan is even, so 2 transfers
    check(debts, 2)
