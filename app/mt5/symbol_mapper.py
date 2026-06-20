from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import MetaTrader5 as mt5

from app.logger import log


@dataclass(frozen=True)
class ResolvedSymbol:
    requested: str
    broker_symbol: str


class SymbolMapper:
    def __init__(self, requested_symbols: Iterable[str]) -> None:
        self.requested_symbols = [symbol.upper() for symbol in requested_symbols]
        self.resolved: Dict[str, str] = {}

    def resolve_all(self) -> Dict[str, str]:
        available = mt5.symbols_get()
        if available is None:
            log.error("symbols_get failed: %s", mt5.last_error())
            return {}

        names = [item.name for item in available]
        names_upper = {name.upper(): name for name in names}

        for requested in self.requested_symbols:
            resolved = self._resolve_one(requested, names, names_upper)
            if resolved and self._select_and_validate(resolved):
                self.resolved[requested] = resolved

        log.info("Requested symbols resolved")
        for requested, broker_symbol in self.resolved.items():
            log.info("%s -> %s", requested, broker_symbol)
        return dict(self.resolved)

    def _resolve_one(self, requested: str, names: List[str], names_upper: Dict[str, str]) -> Optional[str]:
        if requested in names_upper:
            return names_upper[requested]

        suffix_candidates = [f"{requested}m", f"{requested}.", f"{requested}.pro"]
        for candidate in suffix_candidates:
            if candidate.upper() in names_upper:
                return names_upper[candidate.upper()]

        starts = [name for name in names if name.upper().startswith(requested)]
        if starts:
            return starts[0]

        log.warning("Could not resolve requested symbol %s", requested)
        return None

    def _select_and_validate(self, broker_symbol: str) -> bool:
        if not mt5.symbol_select(broker_symbol, True):
            log.warning("symbol_select failed for %s: %s", broker_symbol, mt5.last_error())
            return False

        info = mt5.symbol_info(broker_symbol)
        tick = mt5.symbol_info_tick(broker_symbol)
        if info is None or tick is None:
            log.warning("Symbol validation failed for %s", broker_symbol)
            return False
        return True
