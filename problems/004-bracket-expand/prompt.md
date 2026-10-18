# Bracket Expand

Expand a compact pattern into the full string it describes.

- Lowercase letters `a`-`z` are copied through as they are.
- `k[...]` repeats the expansion of the bracketed part `k` times. `k` is one or
  more digits and can be `0`, which produces nothing.
- `[...]` with no count in front of it is expanded once.
- Brackets nest to any depth.
- A run of digits that is **not** immediately followed by `[` is ordinary text
  and is copied through unchanged, digits and all.

The pattern is always well formed: every `[` has a matching `]`.

## Signature

```python
def expand(pattern: str) -> str:
```

## Examples

```python
expand("3[ab]")       # "ababab"
expand("2[a3[b]]")    # "abbbabbb"
expand("[ab]2[c]")    # "abcc"
expand("a3b")         # "a3b"
expand("a0[xyz]b")    # "ab"
```
