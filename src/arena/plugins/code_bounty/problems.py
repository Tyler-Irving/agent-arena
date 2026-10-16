"""Problem loading and pulling solution code out of model replies."""

import re
from pathlib import Path

from pydantic import BaseModel

PROBLEMS_DIR = Path("problems")


class Problem(BaseModel):
    id: str  # directory name, e.g. "001-two-sum"
    path: Path
    prompt: str


def load_problems(selector: str | None = None, root: Path = PROBLEMS_DIR) -> list[Problem]:
    """All problems in id order, or those matching `selector` ("001" or "001-two-sum")."""
    dirs = sorted(d for d in root.iterdir() if (d / "prompt.md").is_file())
    if selector:
        dirs = [d for d in dirs if d.name == selector or d.name.startswith(f"{selector}-")]
    return [Problem(id=d.name, path=d, prompt=(d / "prompt.md").read_text()) for d in dirs]


_FENCE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str | None:
    """Return the solution code from a model reply, or None if there is no code block.

    Prefers ```python blocks. If there are several, takes the longest, since
    extra blocks are usually short usage examples.
    """
    blocks = [(lang.lower(), body) for lang, body in _FENCE.findall(reply)]
    python = [body for lang, body in blocks if lang in ("python", "py", "python3")]
    candidates = python or [body for _, body in blocks]
    return max(candidates, key=len) if candidates else None
