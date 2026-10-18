import shutil
import subprocess

import pytest

from arena.plugins.code_bounty.grader import IMAGE


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"docker: needs Docker and the {IMAGE} image")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if any(item.get_closest_marker("docker") for item in items) and not _image_available():
        skip = pytest.mark.skip(reason=f"docker or {IMAGE} image not available")
        for item in items:
            if item.get_closest_marker("docker"):
                item.add_marker(skip)
