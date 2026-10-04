"""Write settings into the local `.env` file (where secrets live; it is git-ignored). Other lines are kept."""
from __future__ import annotations

import os
from pathlib import Path


def set_env_values(values: dict[str, str], path: str | Path = ".env") -> None:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    todo = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in todo:
            out.append(f"{key}={todo.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in todo.items()]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
