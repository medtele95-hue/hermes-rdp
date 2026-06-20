"""
JSON-safe serialization utilities for the HERMES local dashboard API.

to_json_safe() converts any Python value to JSON-serializable primitives,
handling circular references, pydantic models, dataclasses, numpy types,
enums, datetimes, and plain objects — without ever crashing.
"""
from __future__ import annotations

import dataclasses as _dataclasses
import enum as _enum
import math as _math
from datetime import date as _date, datetime as _dt
from decimal import Decimal as _Decimal
from pathlib import Path as _Path
from typing import Any

try:
    import numpy as _np  # type: ignore[import]
except ImportError:
    _np = None  # type: ignore[assignment]


def to_json_safe(
    value: Any,
    max_depth: int = 8,
    _anc: frozenset = frozenset(),
) -> Any:
    """
    Recursively convert *value* to JSON-safe primitives only.

    Handles
    -------
    None, bool, int, float, str — returned as-is (or None for NaN/Inf).
    datetime / date              — .isoformat() string.
    enum.Enum                    — recursive on .value.
    pathlib.Path                 — str().
    bytes / bytearray            — UTF-8 decoded string.
    numpy integer / floating     — int / float (None for NaN/Inf).
    numpy ndarray                — list via .tolist().
    decimal.Decimal              — float.
    dict                         — {str(k): recurse(v)}.
    list / tuple / set           — [recurse(v)].
    pydantic v2 model            — recurse(.model_dump()).
    pydantic v1 model            — recurse(.dict()).
    dataclass instance           — recurse(dataclasses.asdict()).
    any object with __dict__     — recurse(vars()).
    everything else              — str() fallback.

    Circular references are detected via ancestor-path tracking.
    Any value whose id() is already in the current ancestor chain is
    replaced with the sentinel string "[CIRCULAR_REF_REMOVED]".
    max_depth prevents unbounded recursion on pathological structures.
    """
    # ---- JSON primitives — return immediately (no id() check needed) --------
    if value is None:
        return None
    if isinstance(value, bool):          # bool is a subclass of int — must be first
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if _math.isnan(value) or _math.isinf(value):
            return None
        return float(value)
    if isinstance(value, str):
        return value

    # ---- Circular-reference / depth guard for containers and objects --------
    try:
        oid = id(value)
        if oid in _anc:
            return "[CIRCULAR_REF_REMOVED]"
        next_anc: frozenset = _anc | {oid}
    except Exception:
        next_anc = _anc

    if max_depth <= 0:
        try:
            return str(value)
        except Exception:
            return None

    nd = max_depth - 1

    def _r(v: Any) -> Any:
        return to_json_safe(v, nd, next_anc)

    # ---- datetime / date ----------------------------------------------------
    if isinstance(value, (_dt, _date)):
        return value.isoformat()

    # ---- enum ---------------------------------------------------------------
    if isinstance(value, _enum.Enum):
        return _r(value.value)

    # ---- Path ---------------------------------------------------------------
    if isinstance(value, _Path):
        return str(value)

    # ---- bytes / bytearray --------------------------------------------------
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return None

    # ---- numpy (optional dependency) ----------------------------------------
    if _np is not None:
        if isinstance(value, _np.integer):
            return int(value)
        if isinstance(value, _np.floating):
            f = float(value)
            return None if (_math.isnan(f) or _math.isinf(f)) else f
        if isinstance(value, _np.bool_):
            return bool(value)
        if isinstance(value, _np.ndarray):
            return [_r(v) for v in value.tolist()]

    # ---- decimal.Decimal ----------------------------------------------------
    if isinstance(value, _Decimal):
        try:
            f = float(value)
            return None if (_math.isnan(f) or _math.isinf(f)) else f
        except Exception:
            return str(value)

    # ---- dict (including OrderedDict and other dict subclasses) -------------
    if isinstance(value, dict):
        result: dict = {}
        for k, v in value.items():
            try:
                result[str(k)] = _r(v)
            except Exception:
                result[str(k)] = None
        return result

    # ---- list / tuple / set / frozenset -------------------------------------
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list = []
        for v in value:
            try:
                out.append(_r(v))
            except Exception:
                out.append(None)
        return out

    # ---- Pydantic v2 --------------------------------------------------------
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump", None)):
        try:
            return _r(value.model_dump())
        except Exception:
            pass

    # ---- Pydantic v1 / .dict() ----------------------------------------------
    if hasattr(value, "dict") and callable(getattr(value, "dict", None)):
        try:
            return _r(value.dict())
        except Exception:
            pass

    # ---- dataclass ----------------------------------------------------------
    if _dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _r(_dataclasses.asdict(value))
        except Exception:
            # asdict can fail if fields contain non-dataclass complex objects;
            # fall through to __dict__ handling below
            pass

    # ---- generic object with __dict__ ---------------------------------------
    if hasattr(value, "__dict__"):
        try:
            return _r(vars(value))
        except Exception:
            pass

    # ---- final fallback -----------------------------------------------------
    try:
        return str(value)
    except Exception:
        return None
