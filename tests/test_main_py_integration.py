from __future__ import annotations

import inspect
from pathlib import Path

from app.agents import setup_hunter
from app.config import Settings
from app.mt5.geometric_engine_v2 import geometric_score


class TestGeometricV2MainIntegration:
    def test_setup_hunter_imports_v2_score(self):
        source = inspect.getsource(setup_hunter)
        assert "from app.mt5.geometric_engine_v2 import geometric_score" in source
        assert "OF_GEO_INSUFFICIENT" in source

    def test_shadow_default_cannot_hard_block_low_geometry(self):
        settings = Settings()
        result = geometric_score(None, None, 0.0, 0.0, 0.0, settings.geometric_mode)
        assert settings.geometric_mode == "SHADOW"
        assert result["decision"] == "WAIT"
        assert result["blockers"] == []

    def test_live_mode_low_geometry_is_blocked(self):
        result = geometric_score(None, None, 0.0, 0.0, 0.0, "LIVE")
        assert result["decision"] == "BLOCK"
        assert result["passes_mode"] is False

    def test_dynamic_exit_logs_target_and_realized_rr(self):
        source = Path("app/mt5/btc_dynamic_exit.py").read_text(encoding="utf-8")
        assert "rr_target" in source
        assert "realized_rr" in source

    def test_no_execution_api_in_v2_or_setup_hunter(self):
        v2_source = Path("app/mt5/geometric_engine_v2.py").read_text(encoding="utf-8")
        hunter_source = Path("app/agents/setup_hunter.py").read_text(encoding="utf-8")
        assert "order_send" not in v2_source
        assert "order_send" not in hunter_source

    def test_safety_defaults_unchanged(self):
        settings = Settings()
        assert settings.demo_only is True
        assert settings.allow_live_trading is False
        assert settings.demo_max_lot == 0.01
