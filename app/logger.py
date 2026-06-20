from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Optional


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("hermes")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)

    return logger


log = configure_logging()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: object, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
