from __future__ import annotations

from app.config import Settings


class ExecutionGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze_only_event(self, decision: dict) -> dict:
        return {
            "symbol": decision.get("symbol"),
            "timeframe": decision.get("timeframe"),
            "event_type": "ANALYSIS_ONLY",
            "status": "BLOCKED_READ_ONLY",
            "magic_number": self.settings.hermes_magic_number,
            "reason": "READ_ONLY=true; trade execution is not implemented in this version",
            "decision": decision.get("decision"),
        }
