# -*- coding: utf-8 -*-
"""COEUR_V2 chantier 1 — rejeu comparatif ancien vs nouveau FINAL_CONFLUENCE.

Le bot n'est redémarré qu'une seule fois à la fin de la mission COEUR_V2 (voir
mission/COEUR_V2.md CLÔTURE) — les décisions déjà dans le dataset ont donc été
calculées avec l'ANCIENNE formule uniquement (pas de champ `new_confluence`/
`old_confluence`, ceux-ci n'apparaissent qu'après le redémarrage). Ce script
rejoue les décisions récentes avec les deux formules sans avoir besoin des
OHLC bruts (non stockés dans le dataset) : `geo_score` est retrouvé par
résolution algébrique de l'ancienne formule elle-même —
`geo_score = final_confluence_score_stocké − smc_contrib − mtfa_contrib − of_bonus`
— où smc_contrib/mtfa_contrib/of_bonus sont recalculés avec le code de
production réel (confirmation_matrix, pas une réimplémentation). Les lignes
où le score stocké est clampé à 0 ou 100 (résolution ambiguë) sont exclues.

Usage: python -m app.tools.coeur_v2_confluence_replay [--limit N]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.agents.confirmation_matrix import (
    mtfa_calibrated_status,
    mtfa_confluence_adjustment,
    smc_calibrated_status,
    smc_confluence_adjustment,
)
from app.agents.confluence_engine import (
    _DEFAULT_WEIGHTS,
    _ORDER_FLOW_NATIVE,
    _SMC_NATIVE,
    _order_flow_bonus,
    _order_flow_bonus_graded,
)

DATASET_PATH = Path(__file__).resolve().parents[2] / "app" / "data" / "decision_dataset.jsonl"


def _load_rows(limit: int | None) -> list[dict]:
    rows = []
    if not DATASET_PATH.exists():
        return rows
    with DATASET_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("row_type") == "decision" and row.get("symbol") in ("GOLD#", "BTCUSD#"):
                rows.append(row)
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[-limit:] if limit else rows


def _extract(row: dict) -> dict | None:
    strategy = str(row.get("strategy") or "").upper()
    legacy_final = row.get("final_confluence_score")
    smc_raw = row.get("smc_score")
    mtfa_raw = row.get("mtfa_score")
    smc_raw = row.get("gate_statuses.smc_score") if smc_raw is None else smc_raw
    mtfa_raw = row.get("gate_statuses.mtfa_score") if mtfa_raw is None else mtfa_raw
    of_score = (
        row.get("order_flow_execution_agent_score")
        or row.get("gate_statuses.order_flow_execution_agent_score")
        or row.get("gold_order_flow_score")
        or row.get("gold_liquidity_score")
    )
    if legacy_final is None or smc_raw is None or mtfa_raw is None:
        return None
    if float(legacy_final) <= 0.0 or float(legacy_final) >= 100.0:
        return None  # clamp boundary — back-solve ambiguous, excluded
    return {
        "symbol": row.get("symbol"),
        "strategy": strategy,
        "decision": row.get("decision"),
        "legacy_final_stored": float(legacy_final),
        "smc_raw": float(smc_raw),
        "mtfa_raw": float(mtfa_raw),
        "of_score": float(of_score) if of_score is not None else None,
        "created_at": row.get("created_at"),
    }


def _legacy_contrib(item: dict) -> tuple[float, float, float, bool]:
    smc_status = smc_calibrated_status(item["smc_raw"])
    mtfa_status = mtfa_calibrated_status(item["mtfa_raw"])
    smc_contrib = smc_confluence_adjustment(smc_status)
    mtfa_contrib = mtfa_confluence_adjustment(mtfa_status)
    of_native = item["strategy"] in _ORDER_FLOW_NATIVE
    of_score = item["of_score"] or 0.0
    if of_native:
        smc_contrib = max(0.0, smc_contrib)
        mtfa_contrib = max(0.0, mtfa_contrib)
        of_bonus = _order_flow_bonus_graded({"order_flow_reader": {"score": of_score, "signal": "BUY"}})
    else:
        of_bonus = _order_flow_bonus({"order_flow_reader": {"score": of_score, "signal": "BUY"}})
    return smc_contrib, mtfa_contrib, of_bonus, of_native


def _v2_score(item: dict, geo_score: float, of_native: bool) -> float:
    # NOTE: BTC RANGE-mode override (floor at 40/35) is intentionally NOT applied here —
    # it needs smc_h4_direction/smc_h1_trend/h1_bias context that isn't stored per-row in
    # the dataset. This makes the replay slightly conservative for BTC STRONG_FAIL rows
    # during RANGE regimes (real production smc_norm would floor higher than this replay's).
    smc_norm = max(0.0, min(100.0, item["smc_raw"]))
    mtfa_norm = max(0.0, min(100.0, item["mtfa_raw"]))
    of_norm = max(0.0, min(100.0, item["of_score"])) if item["of_score"] is not None else 50.0
    if of_native:
        smc_norm = max(50.0, smc_norm)
        mtfa_norm = max(50.0, mtfa_norm)
    w = _DEFAULT_WEIGHTS
    weight_sum = sum(w.values())
    geo_norm = max(0.0, min(100.0, geo_score))
    raw = (w["geo"] * geo_norm + w["smc"] * smc_norm + w["mtfa"] * mtfa_norm + w["of"] * of_norm) / weight_sum
    return max(0.0, min(100.0, round(raw, 2)))


def _threshold_for(strategy: str) -> float:
    if strategy in _ORDER_FLOW_NATIVE:
        return 58.0
    if strategy in _SMC_NATIVE:
        return 65.0
    return 62.0


def replay(limit: int | None) -> dict:
    rows = _load_rows(limit)
    results = []
    excluded = 0
    for row in rows:
        item = _extract(row)
        if item is None:
            excluded += 1
            continue
        smc_c, mtfa_c, of_b, of_native = _legacy_contrib(item)
        geo_score_implied = item["legacy_final_stored"] - smc_c - mtfa_c - of_b
        new_score = _v2_score(item, geo_score_implied, of_native)
        threshold = _threshold_for(item["strategy"])
        legacy_pass = item["legacy_final_stored"] >= threshold
        new_pass = new_score >= threshold
        results.append({
            **item,
            "geo_score_implied": round(geo_score_implied, 2),
            "new_score": new_score,
            "threshold": threshold,
            "legacy_pass": legacy_pass,
            "new_pass": new_pass,
            "flip": "PASS→BLOCK" if legacy_pass and not new_pass else (
                "BLOCK→PASS" if new_pass and not legacy_pass else "UNCHANGED"
            ),
        })

    total = len(results)
    flips = Counter(r["flip"] for r in results)
    legacy_pass_rate = sum(1 for r in results if r["legacy_pass"]) / total * 100.0 if total else 0.0
    new_pass_rate = sum(1 for r in results if r["new_pass"]) / total * 100.0 if total else 0.0

    return {
        "total_replayed": total,
        "excluded_boundary_clamped": excluded,
        "legacy_pass_rate_pct": round(legacy_pass_rate, 1),
        "new_pass_rate_pct": round(new_pass_rate, 1),
        "flips": dict(flips),
        "pass_to_block_examples": [r for r in results if r["flip"] == "PASS→BLOCK"][:10],
        "block_to_pass_examples": [r for r in results if r["flip"] == "BLOCK→PASS"][:10],
        "all_results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = replay(args.limit)
    summary = {k: v for k, v in report.items() if k not in ("all_results", "pass_to_block_examples", "block_to_pass_examples")}
    print(json.dumps(summary, indent=2))
    print("\nPASS->BLOCK examples:")
    for r in report["pass_to_block_examples"]:
        print(f"  {r['symbol']} {r['strategy']} legacy={r['legacy_final_stored']} new={r['new_score']} threshold={r['threshold']}")
    print("\nBLOCK->PASS examples:")
    for r in report["block_to_pass_examples"]:
        print(f"  {r['symbol']} {r['strategy']} legacy={r['legacy_final_stored']} new={r['new_score']} threshold={r['threshold']}")
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
