"""DECISION_DATASET — append-only learning dataset (fail-silent, versioned).

Every routed decision — EXECUTED or REFUSED — is flattened into one JSONL
row in data/decision_dataset.jsonl with:
- schema_version + timestamps,
- every scalar feature of the router event (~200 features: scores and
  components, SMC/MTF flags, of_score, atr, spread_to_atr, net RR, ...),
- ees_sell + band / ees_buy + band, exec quality, account_policy, session,
- regime: h4_bias, atr_percentile (window up to 5000 bars, limited by the
  frames actually available), kill_zone_active,
- momentum_alignment — 3 votes (last 5 CLOSED M1 candles, sign of
  cvd_slope, sign of delta; >=2 agreeing = BULL/BEAR; a missing vote counts
  0 -> NEUTRAL; then ALIGNED / NEUTRAL / AGAINST vs the entry direction).
  PURE SHADOW: zero decisional effect.
- Outcome rows: MFE/MAE tracking + pnl reconciled from MT5 deals for real
  tickets, virtual SL/TP outcome for refused decisions.

Everything is wrapped fail-silent: a dataset failure must never touch the
trading path.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.logger import log
from app.utils.broker_time import from_mt5_deal_time

SCHEMA_VERSION = 2
# COEUR_V2 (2026-07-08, mission/COEUR_V2.md) : version du coeur mathematique/
# geometrique qui a produit cette ligne, distincte de SCHEMA_VERSION (qui suit
# la structure JSON, pas la formule). 1 = pre-COEUR_V2 (confluence brute
# geo-dominante, ATR=SMA, GOLD_RANGE_BREAKOUT inactif). 2 = post-COEUR_V2
# (confluence normalisee ponderee, ATR=Wilder RMA, GOLD_RANGE_BREAKOUT actif,
# strategy_aware par defaut). Frontiere nette pour toute analyse future du
# dataset. Lignes sans ce champ = core_version 1 implicite.
CORE_VERSION = 2
DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "decision_dataset.jsonl"

_KILL_ZONES_UTC = ((7, 9), (12, 14), (1, 3))

# ── outcome tracker persistence + MT5-deal close detection ──────────────────
# (2026-07-13) Deux trous du writer, corriges ensemble ; ZERO impact sur le
# chemin de decision (aucun seuil, aucun gate touche) :
#
# 1. AMNESIE AU REDEMARRAGE — `_open` vivait uniquement en RAM. Tout trade
#    encore ouvert quand le backend redemarrait perdait son suivi et n'etait
#    JAMAIS labellise. L'etat des trades REELS est desormais persiste sur
#    disque (STATE_FILENAME) et recharge au boot.
#    Les positions VIRTUELLES (refus simules) restent volatiles a dessein :
#    elles se comptent en milliers (une par DEMO_SKIP), reecrire ce volume a
#    chaque tick couterait plus cher que ce qu'il rapporte, et ce ne sont pas
#    des trades — seuls les trades reels sont irremplacables.
#
# 2. LABEL SEULEMENT SUR TOUCHE TP/SL — un trade ferme par le trailing, un
#    exit_v2, un preclose news ou un rescue ne touche jamais exactement TP ou
#    SL : aucune ligne outcome n'etait ecrite. Constate sur le dataset : 46
#    des 60 trades backfilles avaient ete fermes par l'EA (DEAL_REASON_EXPERT),
#    pas par TP/SL. La cloture est desormais lue dans les DEALS MT5, qui sont
#    la source de verite (ils portent la raison exacte de la fermeture), le
#    prix de cloture reel et le P&L. Plus aucun seuil "close ~= TP" : c'est
#    MT5 qui dit pourquoi il a ferme.
STATE_FILENAME = "outcome_tracker_state.json"
BROKER_UTC_OFFSET_HOURS = 3.0  # XM ; identique au defaut de app/config.py

# DEAL_ENTRY_IN = 0 ; OUT = 1, INOUT = 2, OUT_BY = 3
_DEAL_ENTRY_OUT = {1, 2, 3}
_DEAL_REASON_NAMES = {
    0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP",
    6: "STOP_OUT", 7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT",
}


def close_info_from_deals(ticket: object, deals_fn) -> dict | None:
    """Etat d'une position REELLE d'apres ses deals MT5.

    -> {"state": "CLOSED", ...} : fermee, avec raison/prix/pnl authentiques.
    -> {"state": "OPEN"}        : les deals prouvent qu'elle est encore ouverte.
    -> None                     : indetermine (pas de deals_fn, aucun deal, ou
                                  deals sans champ `entry` exploitable) — l'appelant
                                  retombe alors sur la detection historique par
                                  touche de prix, jamais sur une invention.

    La distinction OPEN / None est essentielle : si les deals prouvent que la
    position est encore ouverte, on ne DOIT PAS la fermer sur une touche de
    prix (le trailing a pu deplacer le SL — le prix touche l'ancien niveau
    alors que la position vit toujours).
    """
    if deals_fn is None or ticket is None:
        return None
    try:
        deals = list(deals_fn(ticket) or [])
    except Exception:
        return None
    if not deals:
        return None

    entries = [_to_int(getattr(deal, "entry", None)) for deal in deals]
    if all(value is None for value in entries):
        return None  # forme de deal inconnue -> indetermine, pas de conclusion
    outs = [deal for deal, entry in zip(deals, entries) if entry in _DEAL_ENTRY_OUT]
    if not outs:
        return {"state": "OPEN"}

    last = max(outs, key=lambda deal: _to_float(getattr(deal, "time", None)) or 0.0)
    profit = sum(_to_float(getattr(deal, "profit", None)) or 0.0 for deal in deals)
    commission = sum(_to_float(getattr(deal, "commission", None)) or 0.0 for deal in deals)
    swap = sum(_to_float(getattr(deal, "swap", None)) or 0.0 for deal in deals)

    reason_code = _to_int(getattr(last, "reason", None))
    reason_name = _DEAL_REASON_NAMES.get(reason_code, "UNKNOWN") if reason_code is not None else "UNKNOWN"
    if reason_name == "TP":
        label = "TP_HIT"
    elif reason_name == "SL":
        label = "SL_HIT"
    else:
        label = f"CLOSED_{reason_name}"

    closed_at = None
    deal_time = _to_float(getattr(last, "time", None))
    if deal_time:
        try:
            closed_at = from_mt5_deal_time(deal_time, BROKER_UTC_OFFSET_HOURS).isoformat()
        except Exception:
            closed_at = None

    return {
        "state": "CLOSED",
        "outcome": label,
        "close_reason": reason_name,
        "close_price": _to_float(getattr(last, "price", None)),
        "closed_at": closed_at,
        "pnl_reconciled": round(profit + commission + swap, 2),
        "deals_count": len(deals),
    }


# ── momentum alignment (pure shadow feature) ────────────────────────────────

def momentum_alignment(direction: object, m1_rows: list[dict] | None, cvd_slope: object, delta: object) -> dict:
    """3 votes: last 5 CLOSED M1 candles, sign(cvd_slope), sign(delta).
    >=2 agreeing votes -> BULL/BEAR, else NEUTRAL; a missing vote counts 0.
    Then ALIGNED / NEUTRAL / AGAINST versus the entry direction."""
    vote_m1 = 0
    rows = [r for r in (m1_rows or []) if isinstance(r, dict)]
    if len(rows) >= 5:
        closes = [_to_float(r.get("close")) for r in rows[-5:]]
        if all(v is not None for v in closes):
            net = closes[-1] - closes[0]
            vote_m1 = 1 if net > 0 else (-1 if net < 0 else 0)
    cvd = _to_float(cvd_slope)
    vote_cvd = 0 if cvd is None else (1 if cvd > 0 else (-1 if cvd < 0 else 0))
    dlt = _to_float(delta)
    vote_delta = 0 if dlt is None else (1 if dlt > 0 else (-1 if dlt < 0 else 0))

    votes = [vote_m1, vote_cvd, vote_delta]
    bulls = sum(1 for v in votes if v == 1)
    bears = sum(1 for v in votes if v == -1)
    consensus = "BULL" if bulls >= 2 else ("BEAR" if bears >= 2 else "NEUTRAL")

    side = str(direction or "").upper()
    if consensus == "NEUTRAL" or side not in {"BUY", "SELL"}:
        alignment = "NEUTRAL"
    elif (consensus == "BULL") == (side == "BUY"):
        alignment = "ALIGNED"
    else:
        alignment = "AGAINST"
    return {
        "votes": {"m1_last5": vote_m1, "cvd_slope": vote_cvd, "delta": vote_delta},
        "consensus": consensus,
        "alignment": alignment,
    }


# ── regime features ─────────────────────────────────────────────────────────

def atr_percentile(frame: object, period: int = 14, window: int = 5000) -> float | None:
    """Percentile of the current ATR within up to `window` bars of history.
    Limited by the bars actually available (documented limitation)."""
    try:
        if frame is None or getattr(frame, "empty", True) or len(frame) < period + 5:
            return None
        df = frame.tail(window)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr_series = tr.rolling(period).mean().dropna()
        if atr_series.empty:
            return None
        current = float(atr_series.iloc[-1])
        rank = float((atr_series <= current).mean())
        return round(rank * 100.0, 1)
    except Exception:
        return None


def kill_zone_active(now_utc: datetime | None = None) -> bool:
    hour = (now_utc or datetime.now(timezone.utc)).hour
    return any(start <= hour < end for start, end in _KILL_ZONES_UTC)


# ── row building ────────────────────────────────────────────────────────────

_NESTED_FLATTEN = (
    "gate_statuses",
    "exec_quality",
    "account_policy_detail",
    "daily_killswitch",
    "time_gate",
    "order_flow",
    "order_flow_execution_agent",
    "ees_components",
    "max_money_tp",
)


def build_decision_row(event: dict, extras: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    row: dict = {
        "row_type": "decision",
        "schema_version": SCHEMA_VERSION,
        "core_version": CORE_VERSION,
        "recorded_at": now.isoformat(),
    }
    for key, value in (event or {}).items():
        if key in _NESTED_FLATTEN:
            continue
        if _is_scalar(value):
            row[key] = value
    for nested_key in _NESTED_FLATTEN:
        nested = (event or {}).get(nested_key)
        if isinstance(nested, dict):
            for sub_key, sub_value in nested.items():
                if _is_scalar(sub_value):
                    row[f"{nested_key}.{sub_key}"] = sub_value

    extras = extras or {}
    frames = extras.get("frames") if isinstance(extras.get("frames"), dict) else {}
    # regime
    h4 = frames.get("H4") if frames else None
    row["regime.h4_bias"] = event.get("h4_bias") or event.get("smc_h4_direction")
    row["regime.atr_percentile"] = atr_percentile(h4 if h4 is not None else frames.get("M5") if frames else None)
    row["regime.kill_zone_active"] = kill_zone_active(now)
    row["session"] = event.get("session_name") or (event.get("time_gate") or {}).get("session_name") if isinstance(event.get("time_gate"), dict) else event.get("session_name")

    # derived features
    atr_value = _to_float(event.get("atr") or event.get("atr_value"))
    spread = _to_float(event.get("spread") or event.get("spread_at_send_points"))
    row["atr"] = atr_value
    row["spread_to_atr"] = (spread / atr_value) if (spread is not None and atr_value and atr_value > 0) else None
    entry = _to_float(event.get("entry"))
    sl = _to_float(event.get("sl"))
    tp = _to_float(event.get("tp"))
    direction = str(event.get("direction") or "").upper()
    net_rr = None
    if entry is not None and sl is not None and tp is not None:
        risk = (entry - sl) if direction == "BUY" else (sl - entry)
        reward = (tp - entry) if direction == "BUY" else (entry - tp)
        if risk and risk > 0:
            net_rr = round(reward / risk, 3)
    row["net_rr"] = net_rr

    # momentum alignment — PURE SHADOW
    of_payload = event.get("order_flow_execution_agent") if isinstance(event.get("order_flow_execution_agent"), dict) else {}
    m1_rows = extras.get("m1_rows")
    if m1_rows is None and frames:
        m1_frame = frames.get("M1")
        try:
            m1_rows = m1_frame.iloc[:-1].to_dict("records") if m1_frame is not None and not m1_frame.empty else None
        except Exception:
            m1_rows = None
    momentum = momentum_alignment(
        direction,
        m1_rows,
        of_payload.get("cvd_slope") if of_payload else extras.get("cvd_slope"),
        of_payload.get("delta") if of_payload else extras.get("delta"),
    )
    row["momentum_alignment.vote_m1_last5"] = momentum["votes"]["m1_last5"]
    row["momentum_alignment.vote_cvd_slope"] = momentum["votes"]["cvd_slope"]
    row["momentum_alignment.vote_delta"] = momentum["votes"]["delta"]
    row["momentum_alignment.consensus"] = momentum["consensus"]
    row["momentum_alignment.alignment"] = momentum["alignment"]

    for key, value in extras.items():
        if key in {"frames", "m1_rows"}:
            continue
        if _is_scalar(value):
            row[f"extra.{key}"] = value
    return row


# ── dataset writer + outcome tracker ────────────────────────────────────────

class DecisionDataset:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATASET_PATH
        self._lock = threading.Lock()
        # P0-G-2 : cle de deduplication des lignes outcome REELLES. Persistee par
        # OutcomeTracker._save_state, restauree au boot : elle survit donc a un
        # redemarrage, ce qui est exactement le moment ou le doublon apparaissait.
        self.real_outcome_tickets: set[str] = set()
        self.tracker = OutcomeTracker(self)

    def record_decision(self, event: dict, extras: dict | None = None) -> dict | None:
        """Fail-silent: any error is swallowed (logged debug), trading is
        never touched."""
        try:
            row = build_decision_row(event, extras)
            self._append(row)
            self.tracker.register(row)
            return row
        except Exception as exc:  # fail-silent by contract
            try:
                log.debug("[DECISION_DATASET] record_failed error=%s", str(exc)[:200])
            except Exception:
                pass
            return None

    def record_outcome(self, outcome: dict) -> bool:
        """P0-G-2 : IDEMPOTENT par ticket pour les trades REELS.

        Avant, c'etait un simple `_append` : aucune cle de deduplication, aucune
        garde. Un meme ticket pouvait recevoir deux lignes outcome — et un P&L
        double-compte corrompt silencieusement TOUS les agregats (weekly_snapshot,
        daily_report, update_hermes_state lisent `pnl_reconciled`).

        Retourne True si la ligne a ete ecrite, False si c'etait un doublon."""
        try:
            ticket = str(outcome.get("ticket") or "")
            is_real = outcome.get("virtual") is not True and bool(ticket)
            if is_real and ticket in self.real_outcome_tickets:
                log.warning(
                    "[DECISION_DATASET] outcome_duplicate_ignored ticket=%s — "
                    "une ligne outcome existe deja pour ce ticket",
                    ticket,
                )
                return False
            self._append({"row_type": "outcome", "schema_version": SCHEMA_VERSION, "core_version": CORE_VERSION, **outcome})
            if is_real:
                self.real_outcome_tickets.add(ticket)
            return True
        except Exception:
            return False

    def _append(self, row: dict) -> None:
        payload = json.dumps(row, default=str, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(payload + "\n")


class OutcomeTracker:
    """MFE/MAE tracking + outcome for every decision.

    - EXECUTED decisions (real ticket): MFE/MAE updated from ticks; on
      close, pnl reconciled from the MT5 deals history.
    - REFUSED decisions: a VIRTUAL position (entry/sl/tp at refusal) is
      simulated on subsequent ticks -> virtual WIN/LOSS outcome.
    Fail-silent everywhere.
    """

    def __init__(self, dataset: "DecisionDataset", state_path: Path | None = None) -> None:
        self.dataset = dataset
        self._open: dict[str, dict] = {}
        self._counter = 0
        self.state_path = Path(state_path) if state_path else dataset.path.parent / STATE_FILENAME
        self._load_state()

    # ── persistance (trades REELS uniquement — cf. note en tete de module) ──

    def _load_state(self) -> None:
        """Recharge les trades reels encore ouverts. Fail-silent : un etat
        illisible ne doit jamais empecher le bot de demarrer."""
        try:
            if not self.state_path.exists():
                return
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            items = payload.get("open") if isinstance(payload, dict) else None
            restored = 0
            for item in items or []:
                if not isinstance(item, dict) or item.get("virtual") or not item.get("ticket"):
                    continue
                key = str(item.get("key") or f"T{item['ticket']}")
                self._open[key] = item
                restored += 1
            self._counter = int(payload.get("counter") or 0) if isinstance(payload, dict) else 0
            # P0-G-2 : la cle de deduplication survit au redemarrage — c'est
            # precisement le moment ou le doublon apparaissait.
            for ticket in payload.get("outcome_written") or []:
                self.dataset.real_outcome_tickets.add(str(ticket))
            if restored:
                log.info("[DECISION_DATASET] outcome_tracker restaure : %d trade(s) reel(s) ouvert(s)", restored)
        except Exception as exc:
            try:
                log.warning("[DECISION_DATASET] state_load_failed error=%s", str(exc)[:200])
            except Exception:
                pass

    def _save_state(self) -> None:
        """Ecriture atomique (tmp + os.replace) : un crash en plein write ne
        peut pas laisser un etat tronque derriere lui."""
        try:
            real = [item for item in self._open.values() if not item.get("virtual") and item.get("ticket")]
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "counter": self._counter,
                "open": real,
                # P0-G-2 : cle de dedup persistee (bornee : on ne garde que les
                # tickets recents, le fichier ne doit pas grossir sans fin).
                "outcome_written": sorted(self.dataset.real_outcome_tickets)[-500:],
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, default=str)
                os.replace(tmp, self.state_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception as exc:
            try:
                log.debug("[DECISION_DATASET] state_save_failed error=%s", str(exc)[:200])
            except Exception:
                pass

    def register(self, row: dict) -> None:
        try:
            direction = str(row.get("direction") or "").upper()
            entry = _to_float(row.get("entry"))
            sl = _to_float(row.get("sl"))
            tp = _to_float(row.get("tp"))
            if direction not in {"BUY", "SELL"} or entry is None or sl is None or tp is None:
                return
            ticket = row.get("ticket")
            executed = bool(row.get("order_success")) and ticket
            self._counter += 1
            key = f"T{ticket}" if executed else f"V{self._counter}"
            self._open[key] = {
                "key": key,
                "virtual": not executed,
                "ticket": ticket if executed else None,
                "symbol": row.get("broker_symbol") or row.get("symbol"),
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "mfe": 0.0,
                "mae": 0.0,
                "opened_at": row.get("recorded_at"),
                "setup_id": row.get("setup_id"),
                "reason": row.get("reason"),
            }
            if executed:
                self._save_state()
        except Exception:
            pass

    def _outcome_from_deals(self, item: dict, info: dict) -> dict:
        """Ligne outcome batie sur les DEALS MT5 (verite broker) : raison de
        cloture reelle, prix de cloture reel, P&L = profit+commission+swap."""
        base = {
            key: item.get(key)
            for key in ("key", "virtual", "ticket", "symbol", "direction", "entry", "sl", "tp", "mfe", "mae", "opened_at", "setup_id", "reason")
        }
        close_price = info.get("close_price")
        entry = _to_float(item.get("entry"))
        sl = _to_float(item.get("sl"))
        r_multiple = None
        if entry is not None and sl is not None and close_price is not None:
            risk = abs(entry - sl)
            if risk > 0:
                sign = 1.0 if str(item.get("direction") or "").upper() == "BUY" else -1.0
                r_multiple = round(sign * (close_price - entry) / risk, 3)
        pnl = info.get("pnl_reconciled")
        return {
            **base,
            "backfilled": False,
            "closed_at": info.get("closed_at") or datetime.now(timezone.utc).isoformat(),
            "outcome": info.get("outcome"),
            "close_reason": info.get("close_reason"),
            "close_price": close_price,
            "pnl_reconciled": pnl,
            "pnl_source": "MT5_HISTORY_DEALS" if pnl is not None else "UNRESOLVED",
            "r_multiple": r_multiple,
            "win": (pnl > 0) if pnl is not None else None,
            "deals_count": info.get("deals_count"),
        }

    def update(self, prices: dict[str, float], deals_fn=None, now_utc: datetime | None = None) -> list[dict]:
        """prices: {symbol: last_price}. Returns closed outcome rows.

        Trades REELS  : la cloture est lue dans les deals MT5 — TOUTE fermeture
                        est labellisee (trailing, exit_v2, preclose, rescue...),
                        plus seulement une touche exacte de TP/SL.
        Refus VIRTUELS: inchange — simulation TP/SL sur les ticks (ils n'ont
                        aucune position MT5 a interroger).
        """
        closed: list[dict] = []
        dirty = False
        try:
            for key in list(self._open):
                item = self._open[key]
                is_real = not item.get("virtual")

                if is_real:
                    info = close_info_from_deals(item.get("ticket"), deals_fn)
                    if info and info.get("state") == "CLOSED":
                        outcome = self._outcome_from_deals(item, info)
                        # P0-G-2 : on retire le ticket du suivi et on PERSISTE
                        # AVANT d'ecrire la ligne. Auparavant, l'ordre etait
                        # append -> pop -> _save_state en fin de boucle : un crash
                        # entre l'append et la sauvegarde laissait le ticket dans
                        # l'etat, il etait restaure au boot, refermé au cycle
                        # suivant... et une SECONDE ligne outcome etait ecrite pour
                        # le meme ticket (P&L double-compte).
                        #
                        # Le mode de defaillance residuel s'inverse, et c'est
                        # volontaire : un crash entre la sauvegarde et l'append donne
                        # une ligne MANQUANTE (detectable, backfillable) au lieu d'un
                        # DOUBLON (qui corrompt silencieusement tous les agregats).
                        self._open.pop(key, None)
                        self._save_state()   # le ticket n'est plus suivi : au boot,
                                             # il ne sera pas referme => pas de doublon
                        if self.dataset.record_outcome(outcome):
                            closed.append(outcome)
                        self._save_state()   # persiste la cle de dedup
                        dirty = False
                        continue
                    if info and info.get("state") == "OPEN":
                        pass  # position vivante : on continue le suivi MFE/MAE
                    else:
                        # P0-G-1 : les deals ne disent RIEN (MT5 muet, deals_fn
                        # absent, forme inconnue). FAIL-CLOSED : on n'ecrit rien et
                        # on NE RETIRE PAS le ticket. On retentera au prochain cycle.
                        #
                        # Avant, on retombait ici dans la fermeture par touche de
                        # prix : une ligne outcome FAUSSE etait ecrite
                        # (pnl_source=UNRESOLVED) et le ticket sortait du suivi — le
                        # vrai label n'aurait alors JAMAIS ete ecrit. Mon propre
                        # commentaire disait "seuls les deals ferment un trade reel" ;
                        # le code le contredisait exactement quand MT5 etait muet.
                        price = _to_float(prices.get(str(item.get("symbol") or "")))
                        if price is not None:
                            sign = 1.0 if item["direction"] == "BUY" else -1.0
                            excursion = sign * (price - item["entry"])
                            previous = (item["mfe"], item["mae"])
                            item["mfe"] = max(item["mfe"], excursion)
                            item["mae"] = min(item["mae"], excursion)
                            if (item["mfe"], item["mae"]) != previous:
                                dirty = True
                        continue

                price = _to_float(prices.get(str(item.get("symbol") or "")))
                if price is None:
                    continue
                sign = 1.0 if item["direction"] == "BUY" else -1.0
                excursion = sign * (price - item["entry"])
                previous = (item["mfe"], item["mae"])
                item["mfe"] = max(item["mfe"], excursion)
                item["mae"] = min(item["mae"], excursion)
                if is_real and (item["mfe"], item["mae"]) != previous:
                    dirty = True

                # Une position REELLE ne se ferme QUE sur les deals (traite ci-dessus).
                # Ici on n'a plus que des refus VIRTUELS, qui n'ont aucune position MT5
                # a interroger : leur simulation TP/SL sur les ticks est legitime.
                if is_real:
                    continue

                hit_tp = price >= item["tp"] if item["direction"] == "BUY" else price <= item["tp"]
                hit_sl = price <= item["sl"] if item["direction"] == "BUY" else price >= item["sl"]
                if not (hit_tp or hit_sl):
                    continue
                outcome = {
                    **{k: item[k] for k in ("key", "virtual", "ticket", "symbol", "direction", "entry", "sl", "tp", "mfe", "mae", "opened_at", "setup_id", "reason")},
                    "closed_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
                    "outcome": "TP_HIT" if hit_tp else "SL_HIT",
                    "close_price": price,
                }
                if not item["virtual"] and deals_fn is not None:
                    outcome["pnl_reconciled"] = _reconcile_pnl(item["ticket"], deals_fn)
                    outcome["pnl_source"] = "MT5_HISTORY_DEALS" if outcome["pnl_reconciled"] is not None else "UNRESOLVED"
                else:
                    sign_risk = abs(item["entry"] - item["sl"])
                    outcome["virtual_r_multiple"] = round(
                        (sign * (price - item["entry"])) / sign_risk, 3
                    ) if sign_risk > 0 else None
                self.dataset.record_outcome(outcome)
                closed.append(outcome)
                self._open.pop(key, None)
                if is_real:
                    dirty = True
        except Exception:
            pass
        if dirty:
            self._save_state()
        return closed

    def open_count(self) -> int:
        return len(self._open)


def _reconcile_pnl(ticket: object, deals_fn) -> float | None:
    try:
        deals = list(deals_fn(ticket) or [])
        total = 0.0
        found = False
        for deal in deals:
            found = True
            total += _to_float(getattr(deal, "profit", 0)) or 0.0
            total += _to_float(getattr(deal, "commission", 0)) or 0.0
            total += _to_float(getattr(deal, "swap", 0)) or 0.0
        return round(total, 2) if found else None
    except Exception:
        return None


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
