def expand(pattern: str) -> str:
    def parse(i: int) -> tuple[str, int]:
        """Expand from pattern[i] to the matching ']' or the end. Returns (text, index after it)."""
        out: list[str] = []
        while i < len(pattern) and pattern[i] != "]":
            if pattern[i].isdigit():
                j = i
                while j < len(pattern) and pattern[j].isdigit():
                    j += 1
                if j < len(pattern) and pattern[j] == "[":
                    inner, end = parse(j + 1)
                    out.append(inner * int(pattern[i:j]))
                    i = end
                else:
                    out.append(pattern[i:j])
                    i = j
            elif pattern[i] == "[":
                inner, i = parse(i + 1)
                out.append(inner)
            else:
                out.append(pattern[i])
                i += 1
        return "".join(out), i + 1

    return parse(0)[0]
