def sort_versions(versions: list[str]) -> list[str]:
    def key(version: str) -> tuple:
        body = version[1:] if version[:1] in ("v", "V") else version
        release_part, _, pre_part = body.partition("-")
        release = [int(n) for n in release_part.split(".")]
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        if not pre_part:
            return (tuple(release), 1, ())
        pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre_part.split("."))
        return (tuple(release), 0, pre)

    return sorted(versions, key=key)
