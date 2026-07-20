"""A0.3-R6B1 — tests laboratoire de l'émetteur SYSTEM_HEARTBEAT.

Trois blocs, calqués sur le contrat de mission :
1. Flag OFF : zéro heartbeat, flux legacy byte-identical, overhead négligeable.
2. Flag ON : schéma strict, boot_id/cycle_id, throttle monotone, indépendance
   vis-à-vis des setups et de MT5, fail-soft writer, rotation, concurrence.
3. Restart simulé : nouveau boot_id, cycle_id repart, jamais de réutilisation.

Purs : aucun ordre MT5, aucun réseau, aucun fichier hors répertoires
temporaires. Le writer réel (DemoKellyRouter._record_event) est exercé via
events_path temporaire, comme les tests bloc3/bloc4 existants.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter
from app.services.system_heartbeat import (
    SYSTEM_HEARTBEAT_EVENT_TYPE,
    SYSTEM_HEARTBEAT_PRODUCER,
    SystemHeartbeatEmitter,
    build_system_heartbeat_emitter,
)


class FakeMonotonic:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def _emitter(sink: list, *, enabled: bool = True, interval: float = 60.0,
             clock: FakeMonotonic | None = None) -> tuple[SystemHeartbeatEmitter, FakeMonotonic]:
    clock = clock or FakeMonotonic()
    emitter = SystemHeartbeatEmitter(
        enabled=enabled,
        interval_seconds=interval,
        record_event=sink.append,
        mode="DEMO",
        now_monotonic=clock,
    )
    return emitter, clock


def _legacy_event(index: int) -> dict:
    return {
        "event_type": "SETUP_HUNTER",
        "created_at": "2026-07-20T12:00:%02d+00:00" % index,
        "symbol": "GOLD#",
        "grade": "D",
        "index": index,
    }


# ---------------------------------------------------------------------------
# 1. FLAG OFF — comportement legacy strictement inchangé
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_zero_heartbeat_and_zero_state_mutation(self):
        sink: list = []
        emitter, clock = _emitter(sink, enabled=False)
        for _ in range(50):
            clock.advance(120)
            assert emitter.on_cycle_complete(mt5_connected=True) is None
        assert sink == []
        assert emitter.cycle_id == 0
        assert emitter.emitted_count == 0
        assert emitter.error_count == 0

    def test_legacy_stream_byte_identical(self, tmp_path):
        settings = Settings()
        path_a = tmp_path / "a" / "ev.jsonl"
        path_b = tmp_path / "b" / "ev.jsonl"
        router_a = DemoKellyRouter(settings, events_path=path_a)
        router_b = DemoKellyRouter(settings, events_path=path_b)
        emitter, clock = _emitter([], enabled=False)
        emitter._record_event = router_b._record_event  # hook OFF sur le flux B
        for i in range(10):
            router_a._record_event(_legacy_event(i))
            router_b._record_event(_legacy_event(i))
            clock.advance(120)
            emitter.on_cycle_complete(mt5_connected=True)
        assert path_a.read_bytes() == path_b.read_bytes()
        assert SYSTEM_HEARTBEAT_EVENT_TYPE not in path_b.read_text(encoding="utf-8")

    def test_hook_overhead_negligible(self):
        emitter, _ = _emitter([], enabled=False)
        started = time.perf_counter()
        for _ in range(100_000):
            emitter.on_cycle_complete(mt5_connected=True)
        elapsed = time.perf_counter() - started
        # 100 000 appels flag OFF « à vide » : bien sous la milliseconde par
        # cycle réel (le cycle mesuré est ~6-10 s). Marge très large anti-flaky.
        assert elapsed < 1.0


# ---------------------------------------------------------------------------
# 2. FLAG ON — schéma, identité, throttle, indépendance, fail-soft
# ---------------------------------------------------------------------------

class TestFlagOnSchema:
    def test_schema_exact_fields_and_strict_types(self):
        sink: list = []
        emitter, _ = _emitter(sink)
        event = emitter.on_cycle_complete(mt5_connected=True)
        assert event is not None and sink == [event]
        assert set(event) == {
            "event_type", "created_at", "pid", "boot_id", "cycle_id",
            "mode", "producer", "mt5_connected",
        }
        assert event["event_type"] == SYSTEM_HEARTBEAT_EVENT_TYPE
        assert event["producer"] == SYSTEM_HEARTBEAT_PRODUCER
        assert event["mode"] == "DEMO"
        assert type(event["pid"]) is int and event["pid"] == os.getpid()
        assert type(event["cycle_id"]) is int
        assert type(event["mt5_connected"]) is bool
        uuid.UUID(event["boot_id"])  # boot_id = UUID valide
        parsed = datetime.fromisoformat(event["created_at"])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0  # UTC, pas local

    def test_created_at_tracks_utc_now(self):
        sink: list = []
        emitter, _ = _emitter(sink)
        before = datetime.now(timezone.utc)
        event = emitter.on_cycle_complete(mt5_connected=True)
        after = datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(event["created_at"])
        assert before <= parsed <= after

    def test_boot_id_stable_within_process(self):
        sink: list = []
        emitter, clock = _emitter(sink)
        boots = set()
        for _ in range(5):
            event = emitter.on_cycle_complete(mt5_connected=True)
            boots.add(event["boot_id"])
            clock.advance(61)
        assert boots == {emitter.boot_id}

    def test_cycle_id_strictly_increasing(self):
        sink: list = []
        emitter, clock = _emitter(sink)
        for _ in range(6):
            emitter.on_cycle_complete(mt5_connected=True)
            clock.advance(61)
        ids = [e["cycle_id"] for e in sink]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert all(b > a for a, b in zip(ids, ids[1:]))


class TestThrottle:
    def test_first_completed_cycle_always_emits(self):
        sink: list = []
        emitter, _ = _emitter(sink)
        assert emitter.on_cycle_complete(mt5_connected=True) is not None
        assert len(sink) == 1

    def test_no_emission_before_interval(self):
        sink: list = []
        emitter, clock = _emitter(sink)
        emitter.on_cycle_complete(mt5_connected=True)
        clock.advance(59.9)
        assert emitter.on_cycle_complete(mt5_connected=True) is None
        assert len(sink) == 1

    def test_emission_at_interval_boundary(self):
        sink: list = []
        emitter, clock = _emitter(sink)
        emitter.on_cycle_complete(mt5_connected=True)
        clock.advance(60.0)  # borne : elapsed == interval => émission
        assert emitter.on_cycle_complete(mt5_connected=True) is not None
        assert len(sink) == 2

    def test_cycle_id_counts_skipped_cycles(self):
        sink: list = []
        emitter, clock = _emitter(sink)
        emitter.on_cycle_complete(mt5_connected=True)
        for _ in range(5):  # 5 cycles throttlés (~10 s chacun)
            clock.advance(10)
            emitter.on_cycle_complete(mt5_connected=True)
        clock.advance(10)
        event = emitter.on_cycle_complete(mt5_connected=True)
        assert event is not None
        assert event["cycle_id"] == 7  # les cycles non émis comptent quand même


class TestIndependence:
    def test_emitted_without_any_eligible_setup(self, tmp_path):
        # Aucune décision, aucun setup, aucun routage : le heartbeat vit quand
        # même — c'est exactement la faille account_diagnostics corrigée.
        path = tmp_path / "ev.jsonl"
        router = DemoKellyRouter(Settings(), events_path=path)
        clock = FakeMonotonic()
        emitter = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=60,
            record_event=router._record_event, mode="DEMO",
            now_monotonic=clock,
        )
        for _ in range(3):
            emitter.on_cycle_complete(mt5_connected=True)
            clock.advance(61)
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 3
        assert all(e["event_type"] == SYSTEM_HEARTBEAT_EVENT_TYPE for e in lines)

    def test_mt5_disconnected_still_emits(self):
        sink: list = []
        emitter, _ = _emitter(sink)
        event = emitter.on_cycle_complete(mt5_connected=False)
        assert event is not None
        assert event["mt5_connected"] is False  # info d'état, pas preuve de vie


class TestFailSoft:
    def test_writer_exception_never_raises_and_never_fakes_success(self):
        calls = {"n": 0}

        def broken_writer(event: dict) -> None:
            calls["n"] += 1
            raise OSError("disk full (simulated)")

        clock = FakeMonotonic()
        emitter = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=60, record_event=broken_writer,
            mode="DEMO", now_monotonic=clock,
        )
        assert emitter.on_cycle_complete(mt5_connected=True) is None  # pas de levée
        assert emitter.error_count == 1
        assert emitter.emitted_count == 0  # jamais de heartbeat « réussi » fabriqué
        # Le cycle suivant retente immédiatement (état émis non avancé)…
        clock.advance(1)
        assert emitter.on_cycle_complete(mt5_connected=True) is None
        assert calls["n"] == 2
        # … et un writer redevenu sain émet.
        sink: list = []
        emitter._record_event = sink.append
        clock.advance(1)
        assert emitter.on_cycle_complete(mt5_connected=True) is not None
        assert emitter.emitted_count == 1 and len(sink) == 1

    def test_constructor_rejects_non_positive_interval(self):
        for bad in (0, -1, -60.5):
            with pytest.raises(ValueError):
                SystemHeartbeatEmitter(
                    enabled=True, interval_seconds=bad,
                    record_event=lambda e: None, mode="DEMO",
                )

    def test_build_emitter_invalid_config_falls_back_disabled(self):
        settings = Settings(
            hermes_system_heartbeat_enabled=True,
            system_heartbeat_interval_seconds=0,  # invalide
        )
        emitter = build_system_heartbeat_emitter(settings, lambda e: None)
        assert emitter.enabled is False  # fail-closed heartbeat, boot préservé

    def test_build_emitter_default_settings_is_disabled(self):
        emitter = build_system_heartbeat_emitter(Settings(), lambda e: None)
        assert emitter.enabled is False  # OFF par défaut = dormant
        assert emitter.interval_seconds == 60.0


class TestModeConformity:
    """R6B1-R1 — convention unique du champ mode : READ_ONLY | DEMO |
    LIVE_DISABLED, dérivée des settings réels. Jamais de valeur libre/vide."""

    @pytest.mark.parametrize("valid_mode", ["READ_ONLY", "DEMO", "LIVE_DISABLED"])
    def test_allowed_modes_accepted_and_emitted(self, valid_mode):
        sink: list = []
        emitter = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=60,
            record_event=sink.append, mode=valid_mode,
        )
        assert emitter.on_cycle_complete(mt5_connected=True)["mode"] == valid_mode

    @pytest.mark.parametrize("bad_mode", ["", "   ", "LAB.DEMO", "demo", "LIVE", None, 5])
    def test_free_or_empty_mode_rejected(self, bad_mode):
        with pytest.raises(ValueError):
            SystemHeartbeatEmitter(
                enabled=True, interval_seconds=60,
                record_event=lambda e: None, mode=bad_mode,
            )

    def test_builder_derives_read_only_from_default_settings(self):
        emitter = build_system_heartbeat_emitter(Settings(), lambda e: None)
        assert emitter._mode == "READ_ONLY"  # read_only=True par défaut

    def test_builder_derives_demo(self):
        settings = Settings(read_only=False, demo_trading=True, demo_only=True)
        emitter = build_system_heartbeat_emitter(settings, lambda e: None)
        assert emitter._mode == "DEMO"

    def test_builder_derives_live_disabled(self):
        settings = Settings(read_only=False, demo_trading=False)
        emitter = build_system_heartbeat_emitter(settings, lambda e: None)
        assert emitter._mode == "LIVE_DISABLED"


class TestWriterIntegration:
    def test_rotation_next_heartbeat_readable_in_fresh_file(self, tmp_path):
        path = tmp_path / "ev.jsonl"
        router = DemoKellyRouter(Settings(), events_path=path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fichier au-delà du seuil de rotation (20 Mo) : la prochaine écriture rote.
        path.write_bytes(b'{"event_type": "FILLER"}\n' * (21 * 1024 * 1024 // 25))
        emitter = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=60,
            record_event=router._record_event, mode="DEMO",
        )
        event = emitter.on_cycle_complete(mt5_connected=True)
        assert event is not None
        backups = list(tmp_path.glob("ev.*.jsonl"))
        assert len(backups) == 1  # l'ancien fichier a été roté
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # le heartbeat vit seul dans le fichier frais
        assert json.loads(lines[0])["event_type"] == SYSTEM_HEARTBEAT_EVENT_TYPE

    def test_concurrent_append_no_partial_lines(self, tmp_path):
        path = tmp_path / "ev.jsonl"
        router = DemoKellyRouter(Settings(), events_path=path)
        threads, per_thread = 8, 50

        def worker(worker_id: int) -> None:
            clock = FakeMonotonic()
            emitter = SystemHeartbeatEmitter(
                enabled=True, interval_seconds=1,
                record_event=router._record_event, mode="DEMO",
                now_monotonic=clock,
            )
            for i in range(per_thread):
                if i % 2 == 0:
                    emitter.on_cycle_complete(mt5_connected=True)
                    clock.advance(2)
                else:
                    router._record_event(_legacy_event(worker_id * 1000 + i))

        pool = [threading.Thread(target=worker, args=(w,)) for w in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == threads * per_thread
        for line in lines:  # chaque ligne complète et parsable — zéro écriture partielle
            assert json.loads(line)["event_type"] in {SYSTEM_HEARTBEAT_EVENT_TYPE, "SETUP_HUNTER"}


# ---------------------------------------------------------------------------
# 3. RESTART SIMULÉ
# ---------------------------------------------------------------------------

class TestRestart:
    def test_new_boot_id_and_cycle_reset_on_restart(self):
        sink_a: list = []
        first, clock_a = _emitter(sink_a)
        for _ in range(3):
            first.on_cycle_complete(mt5_connected=True)
            clock_a.advance(61)
        sink_b: list = []
        second, _ = _emitter(sink_b)  # nouveau process simulé
        event = second.on_cycle_complete(mt5_connected=True)
        assert second.boot_id != first.boot_id  # jamais réutilisé
        assert event["boot_id"] == second.boot_id
        assert event["cycle_id"] == 1  # repart à zéro dans le nouveau boot
        assert sink_a[-1]["cycle_id"] == 3

    def test_boot_ids_unique_across_many_instances(self):
        boots = {
            SystemHeartbeatEmitter(
                enabled=True, interval_seconds=60,
                record_event=lambda e: None, mode="DEMO",
            ).boot_id
            for _ in range(100)
        }
        assert len(boots) == 100
