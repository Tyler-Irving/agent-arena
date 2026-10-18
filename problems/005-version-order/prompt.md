# Version Order

Sort a list of version strings from lowest to highest.

A version looks like `1.4.2` or `v2.0.0-rc.1`:

- An optional leading `v` or `V`, which is ignored.
- A **release**: one or more non-negative integers separated by dots.
- An optional **pre-release**: a `-` followed by one or more identifiers
  separated by dots. Each identifier is either all digits or contains at least
  one letter.

Ordering rules:

1. Compare releases number by number, as integers (`1.10` is higher than
   `1.9`). Missing trailing numbers count as `0`, so `1.2` and `1.2.0` are equal
   releases.
2. If the releases are equal, a version **with** a pre-release is lower than one
   without: `1.0.0-rc.1` < `1.0.0`.
3. If both have pre-releases, compare identifiers left to right:
   - two all-digit identifiers compare as integers;
   - two identifiers with letters compare as plain strings (ASCII order);
   - an all-digit identifier is lower than one with letters.

   If every identifier compared is equal, the shorter pre-release is lower:
   `1.0.0-alpha` < `1.0.0-alpha.1`.
4. Versions that are still equal keep their original relative order.

Return the original strings, unchanged, in sorted order.

## Signature

```python
def sort_versions(versions: list[str]) -> list[str]:
```

## Examples

```python
sort_versions(["1.10", "1.9", "1.2.3"])        # ["1.2.3", "1.9", "1.10"]
sort_versions(["1.0.0", "1.0.0-rc.1"])         # ["1.0.0-rc.1", "1.0.0"]
sort_versions(["v1.2", "1.2.0", "1.2"])        # ["v1.2", "1.2.0", "1.2"] (all equal)
```
