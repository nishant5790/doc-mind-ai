"""Shared bootstrap: put the repo root on sys.path so `import src.*` works."""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Common asset search paths (tests/, notebooks/, repo root)
ASSET_DIRS = [
    pathlib.Path(__file__).resolve().parent,
    ROOT / "notebooks",
    ROOT,
]


def find_asset(*names: str) -> pathlib.Path:
    """Return the first existing file matching any of `names` in known dirs."""
    for d in ASSET_DIRS:
        for n in names:
            p = d / n
            if p.exists():
                return p
    raise FileNotFoundError(
        f"None of {names} found in {[str(d) for d in ASSET_DIRS]}"
    )
