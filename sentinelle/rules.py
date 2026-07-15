# -*- coding: utf-8 -*-
"""SENTINELLE — LES REGLES (pures, testables, deterministes).

Chaque fonction `check_*` prend des FAITS deja rassembles (dicts/listes) + un
`now` explicite, et renvoie une liste d'`Anomaly`. AUCUNE I/O, AUCUN MT5, AUCUN
appel a l'horloge : tout est injecte. C'est ce qui rend chaque regle testable en
isolation sur des donnees synthetiques (mission : "un cas qui DOIT alerter et un
cas qui NE DOIT PAS").

Regles (mission/MISSION_SENTINELLE.md) :
  A1 RR au fill < 1.5              A2 SL/TP du mauvais cote
  A3 execute malgre BLOCK (ROUGE)  A4 position nue (sans SL ou TP)
  A5 marqueur de version pre-P0TER B1 deal ferme sans outcome > 15 min
  B2 couverture outcomes < 90 %    B3 dataset fige > 2h en session
  B4 compteur de pertes bloque a 0 C1 position orpheline (absente du dataset)
  C2 ecart P&L dataset vs deal MT5 D1 hermes.log muet > 20 min

Principe directeur : ne JAMAIS crier faux. En cas de donnee manquante ou
ambigue, on N'ALERTE PAS (fail-open). "Mieux vaut rater une subtilite que crier
au loup."
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# ── severites / prefixes (mission : prefixe distinct des alertes systeme) ────
RED = "RED"        # 🔴 ALERTE SENTINELLE
ORANGE = "ORANGE"  # 🟠 SENTINELLE

# ── seuils chiffres (tous explicites, jamais devines) ────────────────────────
RR_MIN = 1.5                      # plancher RR du routeur (demo_router._final_rr_floor)
RR_TOLERANCE = 0.01               # marge anti-poussiere-flottante : on alerte si < 1.49
OUTCOME_COVERAGE_MIN_PCT = 90.0   # B2
OUTCOME_MIN_SAMPLE = 5            # B2 : pas de % sur un echantillon minuscule (anti-bruit)
CLOSED_WITHOUT_OUTCOME_MIN = 15   # B1 : minutes
DATASET_STALL_HOURS = 2.0         # B3
LOG_SILENCE_MIN = 20              # D1 : minutes
PNL_TOLERANCE_USD = 0.05          # C2 : tolerance sur l'ecart P&L
EXPECTED_COLLECTION_VERSION = 2   # A5
EXPECTED_CORE_FIX_LEVEL = "P0TER" # A5
KILLSWITCH_LOG_FRESH_MIN = 20     # B4 : la ligne kill-switch doit etre recente pour conclure


@dataclass(frozen=True)
class Anomaly:
    rule: str          # "A1".. "D1"
    severity: str      # RED / ORANGE
    subject: str       # ticket / sujet -> compose la signature anti-bruit
    message: str       # autoportant : QUOI, OU, le CHIFFRE qui prouve

    @property
    def signature(self) -> str:
        return f"{self.rule}:{self.subject}"


# ── helpers numeriques (tolerants : une valeur illisible -> None -> pas d'alerte) ─
def _f(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _direction(row: dict) -> str:
    return str(row.get("direction") or "").upper()


def _fill_or_entry(row: dict) -> float | None:
    """Prix de reference d'execution : le FILL reel si connu, sinon l'entree signal."""
    return _f(row.get("exec_quality.fill_price")) if row.get("exec_quality.fill_price") is not None else _f(row.get("entry"))


def rr_at_price(direction: str, price: float | None, sl: float | None, tp: float | None) -> float | None:
    """RR = reward/risk oriente. None si non calculable ou geometrie inversee
    (risk <= 0) — ce dernier cas est le domaine de A2, pas de A1."""
    if None in (price, sl, tp):
        return None
    if direction == "BUY":
        risk, reward = price - sl, tp - price
    elif direction == "SELL":
        risk, reward = sl - price, price - tp
    else:
        return None
    if risk <= 0:
        return None
    return reward / risk


# ═════════════════════════════════════════════════════════════════════════════
# A — INTEGRITE DES OUVERTURES (sur les NOUVEAUX trades executes depuis la passe)
# ═════════════════════════════════════════════════════════════════════════════

def check_A1_rr_fill(new_executed: list[dict]) -> list[Anomaly]:
    """A1 : un trade execute avec RR AU FILL < 1.5. Le routeur garantit RR>=1.5
    sur les valeurs de DECISION ; un RR au fill sous le plancher revele un
    slippage anormal ou une regression du plancher."""
    out = []
    for row in new_executed:
        price = _fill_or_entry(row)
        rr = rr_at_price(_direction(row), price, _f(row.get("sl")), _f(row.get("tp")))
        if rr is not None and rr < RR_MIN - RR_TOLERANCE:
            out.append(Anomaly(
                "A1", RED, str(row.get("ticket")),
                f"Trade #{row.get('ticket')} ({_direction(row)} {row.get('symbol')}) : "
                f"RR au fill = {rr:.3f} < {RR_MIN} (fill={price}, sl={row.get('sl')}, tp={row.get('tp')}).",
            ))
    return out


def check_A2_sltp_side(new_executed: list[dict]) -> list[Anomaly]:
    """A2 : SL/TP du mauvais cote. BUY exige sl < entry < tp ; SELL exige
    tp < entry < sl. Un ordre execute avec cette geometrie est certainement faux."""
    out = []
    for row in new_executed:
        d = _direction(row)
        entry, sl, tp = _f(row.get("entry")), _f(row.get("sl")), _f(row.get("tp"))
        if None in (entry, sl, tp) or d not in ("BUY", "SELL"):
            continue
        valid = (sl < entry < tp) if d == "BUY" else (tp < entry < sl)
        if not valid:
            out.append(Anomaly(
                "A2", RED, str(row.get("ticket")),
                f"Trade #{row.get('ticket')} ({d} {row.get('symbol')}) : SL/TP du mauvais cote "
                f"(entry={entry}, sl={sl}, tp={tp}).",
            ))
    return out


def check_A3_executed_despite_block(new_executed: list[dict]) -> list[Anomaly]:
    """A3 — ROUGE PRIORITAIRE : un trade EXECUTE alors que fallback_decision=BLOCK.
    C'est la faille exacte qui a cause la perte v1 (le fallback top-down qui
    passait outre un BLOCK). P0-A doit l'empecher ; toute recidive est critique."""
    out = []
    for row in new_executed:
        if str(row.get("fallback_decision") or "").upper() == "BLOCK":
            reason = row.get("fallback_block_reason") or row.get("fallback_reason") or "?"
            out.append(Anomaly(
                "A3", RED, str(row.get("ticket")),
                f"Trade #{row.get('ticket')} ({_direction(row)} {row.get('symbol')}) : EXECUTE malgre "
                f"fallback_decision=BLOCK ({reason}). C'est la faille v1 — verifier P0-A immediatement.",
            ))
    return out


def check_A4_naked_positions(open_positions: list[dict]) -> list[Anomaly]:
    """A4 : position ouverte chez le broker SANS SL ou SANS TP (ordre nu).
    SL/TP a 0.0 = absent cote MT5."""
    out = []
    for pos in open_positions:
        sl, tp = _f(pos.get("sl")), _f(pos.get("tp"))
        if sl is None or tp is None or sl == 0.0 or tp == 0.0:
            out.append(Anomaly(
                "A4", RED, str(pos.get("ticket")),
                f"Position #{pos.get('ticket')} ({pos.get('symbol')}) NUE : "
                f"sl={pos.get('sl')} tp={pos.get('tp')} — ordre sans protection.",
            ))
    return out


def check_A5_version_marker(new_executed: list[dict]) -> list[Anomaly]:
    """A5 : un nouveau trade dont collection_version != 2 OU core_fix_level absent.
    Une ligne pre-P0TER ne doit plus s'ecrire ; sa presence = regression du writer."""
    out = []
    for row in new_executed:
        cv = row.get("collection_version")
        cfl = row.get("core_fix_level")
        bad_cv = (cv != EXPECTED_COLLECTION_VERSION)
        bad_cfl = (cfl is None or str(cfl) == "")
        if bad_cv or bad_cfl:
            out.append(Anomaly(
                "A5", ORANGE, str(row.get("ticket")),
                f"Trade #{row.get('ticket')} ecrit avec un marqueur pre-P0TER : "
                f"collection_version={cv} (attendu {EXPECTED_COLLECTION_VERSION}), "
                f"core_fix_level={cfl!r} (attendu {EXPECTED_CORE_FIX_LEVEL!r}).",
            ))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# B — SANTE DE LA COLLECTE V2
# ═════════════════════════════════════════════════════════════════════════════

def check_B1_closed_without_outcome(
    closing_deals: list[dict], outcomes_by_ticket: dict, now: datetime
) -> list[Anomaly]:
    """B1 : un deal de cloture (magic 909002) dont la position n'a AUCUNE ligne
    outcome dans le dataset apres > 15 min. Le trou des 61 outcomes perdus,
    plus jamais."""
    out = []
    for deal in closing_deals:
        ticket = str(deal.get("position_id"))
        close_time = deal.get("close_time")
        if close_time is None:
            continue
        age_min = (now - close_time).total_seconds() / 60.0
        if age_min > CLOSED_WITHOUT_OUTCOME_MIN and ticket not in outcomes_by_ticket:
            out.append(Anomaly(
                "B1", RED, ticket,
                f"Position #{ticket} fermee depuis {age_min:.0f} min (deal MT5) mais SANS ligne "
                f"outcome dans le dataset. Le writer n'a pas labellise cette cloture.",
            ))
    return out


def check_B2_outcome_coverage(executed_24h: list[dict], outcomes_by_ticket: dict) -> list[Anomaly]:
    """B2 : % de trades executes AVEC outcome sur 24h < 90 %. On exige un
    echantillon minimal (>=5) pour ne pas alerter sur 0/1 ou 1/2 (anti-bruit)."""
    total = len(executed_24h)
    if total < OUTCOME_MIN_SAMPLE:
        return []
    with_outcome = sum(1 for r in executed_24h if str(r.get("ticket")) in outcomes_by_ticket)
    pct = 100.0 * with_outcome / total
    if pct < OUTCOME_COVERAGE_MIN_PCT:
        return [Anomaly(
            "B2", ORANGE, "coverage_24h",
            f"Couverture outcomes 24h = {pct:.0f}% ({with_outcome}/{total}) < {OUTCOME_COVERAGE_MIN_PCT:.0f}%. "
            f"Des trades executes restent non labellises.",
        )]
    return []


def check_B3_dataset_stall(
    last_decision_time: datetime | None, now: datetime,
    market_open: bool, killswitch_triggered: bool,
) -> list[Anomaly]:
    """B3 : le dataset n'a pas grossi depuis > 2h PENDANT une session de trading.
    Exclu si marche ferme (week-end flat) ou kill-switch declenche (arret voulu)."""
    if not market_open or killswitch_triggered or last_decision_time is None:
        return []
    age_h = (now - last_decision_time).total_seconds() / 3600.0
    if age_h > DATASET_STALL_HOURS:
        return [Anomaly(
            "B3", ORANGE, "dataset_stall",
            f"Aucune decision ecrite depuis {age_h:.1f}h en session ouverte "
            f"(derniere : {last_decision_time.isoformat()}). Writer fige ?",
        )]
    return []


def check_B4_counter_stuck(
    deals_losses_today: int | None, logged_losses: int | None,
    logged_daily_pnl: float | None, killswitch_log_age_min: float | None,
) -> list[Anomaly]:
    """B4 : compteur de pertes bloque a 0 alors que des pertes existent (regression
    des stops P0-D). On ne conclut QUE si la ligne kill-switch est recente (le bot
    evalue bien) ET rapporte losses=0/pnl=0 tandis que les deals montrent des pertes."""
    if deals_losses_today is None or deals_losses_today <= 0:
        return []
    if logged_losses is None or killswitch_log_age_min is None:
        return []  # pas de ligne kill-switch lisible -> on ne conclut pas
    if killswitch_log_age_min > KILLSWITCH_LOG_FRESH_MIN:
        return []  # ligne trop vieille -> le bot n'a peut-etre pas encore recompte
    pnl_zero = (logged_daily_pnl is None) or (abs(logged_daily_pnl) < 0.005)
    if logged_losses == 0 and pnl_zero:
        return [Anomaly(
            "B4", RED, "loss_counter",
            f"Compteur de pertes bloque : les deals MT5 montrent {deals_losses_today} perte(s) aujourd'hui, "
            f"mais le kill-switch logue losses=0 / daily_pnl=0. Regression possible des stops P0-D.",
        )]
    return []


# ═════════════════════════════════════════════════════════════════════════════
# C — COHERENCE BROKER <-> SYSTEME
# ═════════════════════════════════════════════════════════════════════════════

def check_C1_orphan_positions(open_positions: list[dict], known_tickets: set) -> list[Anomaly]:
    """C1 : une position ouverte chez le broker (magic 909002) dont le ticket est
    ABSENT du dataset. Position orpheline = trade que le systeme ne connait pas."""
    out = []
    for pos in open_positions:
        ticket = str(pos.get("ticket"))
        if ticket not in known_tickets:
            out.append(Anomaly(
                "C1", RED, ticket,
                f"Position #{ticket} ({pos.get('symbol')}, magic {pos.get('magic')}) ouverte chez le broker "
                f"mais ABSENTE du dataset. Orpheline — le systeme l'ignore.",
            ))
    return out


def check_C2_pnl_mismatch(
    outcomes_by_ticket: dict, mt5_pnl_by_ticket: dict
) -> list[Anomaly]:
    """C2 : ecart entre le P&L cote dataset (pnl_reconciled) et le P&L du deal MT5
    correspondant > tolerance. Detecte un bug de signe/affichage. Seuls les
    outcomes reconcilies depuis les deals MT5 sont compares (source homogene)."""
    out = []
    for ticket, oc in outcomes_by_ticket.items():
        ds_pnl = _f(oc.get("pnl_reconciled"))
        mt5_pnl = _f(mt5_pnl_by_ticket.get(ticket))
        if ds_pnl is None or mt5_pnl is None:
            continue
        diff = abs(ds_pnl - mt5_pnl)
        if diff > PNL_TOLERANCE_USD:
            out.append(Anomaly(
                "C2", RED, str(ticket),
                f"Trade #{ticket} : P&L dataset={ds_pnl:.2f} vs deal MT5={mt5_pnl:.2f} "
                f"(ecart {diff:.2f} > {PNL_TOLERANCE_USD}). Bug de signe/affichage ?",
            ))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# D — VIE DU SYSTEME (complete le watchdog, ne le double pas : ACTIVITE decisionnelle)
# ═════════════════════════════════════════════════════════════════════════════

def check_D1_log_silence(log_mtime: datetime | None, now: datetime) -> list[Anomaly]:
    """D1 : aucune ecriture dans hermes.log depuis > 20 min. Le watchdog gere le
    PROCESS ; la sentinelle verifie l'ACTIVITE decisionnelle (le bot ecrit-il ?)."""
    if log_mtime is None:
        return []
    age_min = (now - log_mtime).total_seconds() / 60.0
    if age_min > LOG_SILENCE_MIN:
        return [Anomaly(
            "D1", ORANGE, "log_silence",
            f"hermes.log muet depuis {age_min:.0f} min (> {LOG_SILENCE_MIN}). "
            f"Le bot n'ecrit plus de decision — muet ?",
        )]
    return []


# ── combinateur : rassemble toutes les regles sur un paquet de faits ─────────
def evaluate_all(facts: dict, now: datetime) -> list[Anomaly]:
    """Applique toutes les regles. `facts` est rassemble par sentinelle.py (I/O
    + MT5) ; ici tout est pur. Une cle de fait absente => la regle concernee ne
    trouve rien (fail-open)."""
    a: list[Anomaly] = []
    new_exec = facts.get("new_executed", [])
    a += check_A1_rr_fill(new_exec)
    a += check_A2_sltp_side(new_exec)
    a += check_A3_executed_despite_block(new_exec)
    a += check_A4_naked_positions(facts.get("open_positions", []))
    a += check_A5_version_marker(new_exec)
    a += check_B1_closed_without_outcome(
        facts.get("closing_deals", []), facts.get("outcomes_by_ticket", {}), now)
    a += check_B2_outcome_coverage(
        facts.get("executed_24h", []), facts.get("outcomes_by_ticket", {}))
    a += check_B3_dataset_stall(
        facts.get("last_decision_time"), now,
        facts.get("market_open", False), facts.get("killswitch_triggered", False))
    a += check_B4_counter_stuck(
        facts.get("deals_losses_today"), facts.get("logged_losses"),
        facts.get("logged_daily_pnl"), facts.get("killswitch_log_age_min"))
    a += check_C1_orphan_positions(
        facts.get("open_positions", []), facts.get("known_tickets", set()))
    a += check_C2_pnl_mismatch(
        facts.get("outcomes_by_ticket", {}), facts.get("mt5_pnl_by_ticket", {}))
    a += check_D1_log_silence(facts.get("log_mtime"), now)
    return a
