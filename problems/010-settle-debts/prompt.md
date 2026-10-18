# Settle Debts

A group of friends records who owes whom. Each record `(debtor, creditor,
amount)` means `debtor` owes `creditor` `amount` cents. The same pair can appear
many times, in either direction, and a record where `debtor == creditor` means
nothing.

Work out each person's net balance from all the records, then return a list of
transfers `(payer, payee, amount)` that brings every balance to zero, using the
**fewest transfers possible**. Every transfer amount must be a positive
integer, and a person never pays themselves.

People whose net balance is already zero must not appear in any transfer.

If several answers use the fewest transfers, any of them is accepted. At most
12 people have a non-zero balance, and the function must answer in well under
a second for 12, which is too many to try every possible set of transfers.
Simply matching the largest debtor with the largest creditor does not always
give the fewest transfers.

## Signature

```python
def settle(debts: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
```

## Examples

```python
settle([("ann", "bob", 500)])
# [("ann", "bob", 500)]

settle([("ann", "bob", 500), ("bob", "cat", 500)])
# [("ann", "cat", 500)]: bob is even

settle([("ann", "bob", 300), ("bob", "ann", 300)])
# []
```
