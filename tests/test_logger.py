# -*- coding: utf-8 -*-
"""Régression auto-médecin 2026-07-08 : un rollover en échec (fichier verrouillé
par un ancien process encore en train de mourir) ne doit plus rendre le logger
définitivement muet."""
from __future__ import annotations

import logging
from unittest.mock import patch

from app.logger import SafeRotatingFileHandler


def test_failed_rollover_does_not_raise(tmp_path):
    log_file = tmp_path / "hermes.log"
    handler = SafeRotatingFileHandler(str(log_file), maxBytes=10, backupCount=2, encoding="utf-8")
    with patch.object(logging.handlers.RotatingFileHandler, "doRollover", side_effect=OSError("locked")):
        handler.doRollover()  # ne doit pas lever
    record = logging.LogRecord("hermes", logging.INFO, __file__, 1, "still alive", None, None)
    handler.emit(record)  # le handler continue d'ecrire apres l'echec de rollover
    handler.close()
    assert "still alive" in log_file.read_text(encoding="utf-8")


def test_successful_rollover_still_works(tmp_path):
    log_file = tmp_path / "hermes.log"
    handler = SafeRotatingFileHandler(str(log_file), maxBytes=10, backupCount=2, encoding="utf-8")
    handler.doRollover()  # pas de fichier a tourner, ne doit pas lever non plus
    handler.close()
