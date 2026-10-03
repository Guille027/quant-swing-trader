from __future__ import annotations

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"qsts.{name}")
    if not logging.getLogger("qsts").handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger("qsts")
        root.addHandler(h)
        root.setLevel(logging.INFO)
    return logger
