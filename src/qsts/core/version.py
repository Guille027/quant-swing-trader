"""Version of the running code (git commit, '-dirty' if the source was edited), shown in the app's footer."""
from __future__ import annotations

import subprocess
from pathlib import Path


def code_version() -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "src"],
                               capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return sha.stdout.strip()[:12] + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    from importlib.metadata import version
    return "pkg-" + version("qsts")
