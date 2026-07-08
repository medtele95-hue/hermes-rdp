from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ═════════════════════════════════════════════════════════════════════════
# HERMES_LOG_FILE — INVARIANT PERMANENT (mission FIX_BTC point 8, 2026-07-08).
# Le bot ne tourne JAMAIS sans fichier de log. Le chemin par défaut est EN DUR
# (logs/hermes.log à la racine du repo) ; la variable d'environnement
# HERMES_LOG_FILE peut le déplacer mais ne peut PAS le désactiver.
# Rotation : 10 MB × 5 fichiers — le log ne mange jamais le disque.
# Fail-safe : si le fichier est inécrivable, la console reste active et le
# bot continue (jamais de crash à cause du logging).
# ═════════════════════════════════════════════════════════════════════════
_DEFAULT_LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "hermes.log"


def _resolve_log_file() -> Path:
    override = str(os.getenv("HERMES_LOG_FILE") or "").strip()
    return Path(override) if override else _DEFAULT_LOG_FILE


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("hermes")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)

    has_file = any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers)
    if not has_file:
        try:
            log_file = _resolve_log_file()
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            logger.addHandler(file_handler)
        except OSError as exc:  # fail-safe: console survives, bot never dies here
            logger.warning("[LOG_FILE_UNAVAILABLE] path=%s error=%s", _resolve_log_file(), exc)

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
