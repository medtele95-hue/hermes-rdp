#!/usr/bin/env python3
"""
HERMES DEMO MONITOR — Surveillance intelligente continue
Tourne en parallèle de main.py pendant toute la période démo.
Lit demo_pilot_events.jsonl en temps réel et analyse chaque événement.
"""

import json
import time
import os
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────────────────────
EVENTS_FILE   = r"C:\hermes-mt5-agent\app\data\demo_pilot_events.jsonl"
REPORT_FILE   = r"C:\hermes-mt5-agent\audit\demo_monitor_report.md"
ALERT_FILE    = r"C:\hermes-mt5-agent\audit\demo_alerts.txt"
CHECK_INTERVAL = 5  # secondes entre chaque lecture

# Seuils de validation des 68%
SEUIL_WIN_RATE_MIN      = 0.45   # win rate minimum acceptable
SEUIL_RR_MIN            = 1.2    # RR réalisé minimum
SEUIL_CONFLUENCE_MIN    = 38.0   # score confluence minimum pour trader
SEUIL_DRAWDOWN_MAX_USD  = 15.0   # drawdown max sur session démo
SEUIL_TRADES_CONSEC_NEG = 3      # max trades négatifs consécutifs

# ── ÉTAT GLOBAL ──────────────────────────────────────────────────────
state = {
    "trades":               [],       # tous les trades clôturés
    "open_positions":       {},       # positions ouvertes {ticket: data}
    "session_pnl":          0.0,      # P&L de la session
    "consecutive_losses":   0,        # pertes consécutives
    "last_line_count":      0,        # nb lignes lues
    "alerts":               [],       # alertes déclenchées
    "strategy_stats":       defaultdict(lambda: {"wins":0,"losses":0,"pnl":0.0}),
    "confluence_at_entry":  [],       # scores de confluence au moment des entrées
    "mss_confirmed_count":  0,        # nb trades avec MSS confirmé
    "kill_zone_count":      0,        # nb trades en kill zone
    "h1_aligned_count":     0,        # nb trades avec H1 aligné
}

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")

def alert(msg, critical=False):
    level = "CRITIQUE" if critical else "ALERTE"
    full = f"[{datetime.now(timezone.utc).isoformat()}] {level} — {msg}"
    state["alerts"].append(full)
    log(msg, level="ALERT")
    with open(ALERT_FILE, "a", encoding="utf-8") as f:
        f.write(full + "\n")
    if critical:
        print("\n" + "=" * 60)
        print(f"  {level}")
        print(f"  {msg}")
        print("=" * 60 + "\n")

def parse_event(line):
    try:
        return json.loads(line.strip())
    except Exception:
        return None

def analyze_trade_entry(event):
    """Analyse une entrée de trade et valide les confirmations."""
    symbol    = event.get("symbol", "?")
    strategy  = event.get("strategy", "?")
    direction = event.get("direction", "?")
    score     = event.get("confluence_score", 0)
    mss       = event.get("mss_confirmed", False)
    kill_zone = event.get("in_kill_zone", False)
    h1_bias   = event.get("h1_bias", "NEUTRAL")
    sfp       = event.get("sfp_confirmed", False)
    fvg       = event.get("fvg_active", False)

    log(f"TRADE ENTRE — {symbol} {direction} | score={score} | "
        f"mss={mss} | kz={kill_zone} | h1={h1_bias} | sfp={sfp} | fvg={fvg}")

    confirmations_ok = 0
    confirmations_total = 6

    if score >= SEUIL_CONFLUENCE_MIN:
        confirmations_ok += 1
    else:
        alert(f"Entree avec score faible {score} < {SEUIL_CONFLUENCE_MIN} | {symbol} {direction}")

    if mss:
        confirmations_ok += 1
        state["mss_confirmed_count"] += 1
    else:
        log(f"  -> MSS non confirme sur {symbol} {direction}")

    if kill_zone:
        confirmations_ok += 1
        state["kill_zone_count"] += 1
    else:
        log(f"  -> Entree hors kill zone sur {symbol}")

    if h1_bias != "NEUTRAL" and (
        (h1_bias == "BULLISH" and direction == "BUY") or
        (h1_bias == "BEARISH" and direction == "SELL")
    ):
        confirmations_ok += 1
        state["h1_aligned_count"] += 1
    else:
        log(f"  -> H1 non aligne : h1={h1_bias} direction={direction}")

    if sfp:
        confirmations_ok += 1
    if fvg:
        confirmations_ok += 1

    state["confluence_at_entry"].append(score)

    quality = "FORT"   if confirmations_ok >= 4 \
         else "MOYEN"  if confirmations_ok >= 2 \
         else "FAIBLE"

    log(f"  -> QUALITE ENTREE : {quality} ({confirmations_ok}/{confirmations_total} confirmations)")

    return {
        "symbol": symbol, "strategy": strategy, "direction": direction,
        "score": score, "confirmations": confirmations_ok,
        "mss": mss, "kill_zone": kill_zone, "h1_bias": h1_bias,
        "sfp": sfp, "fvg": fvg, "quality": quality,
    }

def analyze_trade_close(event):
    """Analyse une clôture de trade."""
    ticket    = event.get("ticket")
    pnl       = float(event.get("pnl", 0))
    rr        = float(event.get("realized_rr", 0))
    closed_by = event.get("closed_by", "UNKNOWN")
    strategy  = event.get("strategy", "?")

    state["session_pnl"] += pnl
    state["strategy_stats"][strategy]["pnl"] += pnl

    if pnl > 0:
        state["consecutive_losses"] = 0
        state["strategy_stats"][strategy]["wins"] += 1
        log(f"TRADE GAGNE  +{pnl:.2f}$ | RR={rr:.2f} | closed_by={closed_by}")
    else:
        state["consecutive_losses"] += 1
        state["strategy_stats"][strategy]["losses"] += 1
        log(f"TRADE PERDU  {pnl:.2f}$ | RR={rr:.2f} | closed_by={closed_by}")

    state["trades"].append({
        "ticket": ticket, "pnl": pnl, "rr": rr,
        "closed_by": closed_by, "strategy": strategy,
    })

    if state["consecutive_losses"] >= SEUIL_TRADES_CONSEC_NEG:
        alert(
            f"{state['consecutive_losses']} pertes consecutives — "
            f"verifier la qualite des signaux",
            critical=(state["consecutive_losses"] >= 5)
        )

    if abs(state["session_pnl"]) > SEUIL_DRAWDOWN_MAX_USD and state["session_pnl"] < 0:
        alert(
            f"DRAWDOWN SESSION = {state['session_pnl']:.2f}$ "
            f"— depasse le seuil de {SEUIL_DRAWDOWN_MAX_USD}$",
            critical=True
        )

    if rr > 0 and rr < SEUIL_RR_MIN:
        alert(
            f"RR realise {rr:.2f} < {SEUIL_RR_MIN} — "
            f"gestionnaire de sortie coupe trop tot ? (closed_by={closed_by})"
        )

    if closed_by == "HERMES_QUICK_EXIT_MANAGER" and pnl < 0:
        alert(f"QuickExitManager a cloture en perte {pnl:.2f}$ — F-07 toujours actif ?")

def compute_stats():
    """Calcule les statistiques globales."""
    trades = state["trades"]
    if not trades:
        return {}

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    rrs    = [t["rr"] for t in trades if t["rr"] > 0]

    win_rate  = len(wins) / len(trades) if trades else 0
    avg_win   = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    avg_rr    = sum(rrs) / len(rrs) if rrs else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    confluence_avg = (
        sum(state["confluence_at_entry"]) / len(state["confluence_at_entry"])
        if state["confluence_at_entry"] else 0
    )
    total_entries = len(state["confluence_at_entry"]) or 1

    return {
        "total_trades":       len(trades),
        "win_rate":           win_rate,
        "avg_win_usd":        avg_win,
        "avg_loss_usd":       avg_loss,
        "avg_rr":             avg_rr,
        "expectancy":         expectancy,
        "session_pnl":        state["session_pnl"],
        "consecutive_losses": state["consecutive_losses"],
        "confluence_avg":     confluence_avg,
        "mss_rate":           state["mss_confirmed_count"] / total_entries,
        "kill_zone_rate":     state["kill_zone_count"]     / total_entries,
        "h1_aligned_rate":    state["h1_aligned_count"]    / total_entries,
        "alerts_count":       len(state["alerts"]),
    }

def validation_68pct(stats):
    """Valide si les 68% de complétude sont atteints."""
    if not stats:
        return []

    checks = [
        ("Win rate >= 45%",
         stats["win_rate"] >= SEUIL_WIN_RATE_MIN,
         f"{stats['win_rate']*100:.1f}%"),

        ("RR realise moyen >= 1.2",
         stats["avg_rr"] >= SEUIL_RR_MIN,
         f"{stats['avg_rr']:.2f}"),

        ("Expectancy positive",
         stats["expectancy"] > 0,
         f"{stats['expectancy']:.2f}$"),

        ("MSS confirme > 50% des trades",
         stats["mss_rate"] >= 0.50,
         f"{stats['mss_rate']*100:.0f}%"),

        ("Kill zone > 60% des trades",
         stats["kill_zone_rate"] >= 0.60,
         f"{stats['kill_zone_rate']*100:.0f}%"),

        ("H1 aligne > 55% des trades",
         stats["h1_aligned_rate"] >= 0.55,
         f"{stats['h1_aligned_rate']*100:.0f}%"),

        ("Confluence moyenne >= 38",
         stats["confluence_avg"] >= SEUIL_CONFLUENCE_MIN,
         f"{stats['confluence_avg']:.1f}"),

        ("Drawdown session < 15$",
         stats["session_pnl"] > -SEUIL_DRAWDOWN_MAX_USD,
         f"{stats['session_pnl']:.2f}$"),

        ("Pas d'alerte critique active",
         stats["alerts_count"] == 0,
         f"{stats['alerts_count']} alertes"),
    ]

    return checks

def write_report(stats, checks):
    """Ecrit le rapport de surveillance dans audit/."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not stats:
        return

    checks_ok  = sum(1 for _, ok, _ in checks if ok)
    checks_tot = len(checks)
    pct        = int(checks_ok / checks_tot * 100)

    verdict = (
        "VALIDE — PRET POUR LE LIVE" if pct >= 80
        else "EN COURS — CONTINUER LA DEMO" if pct >= 50
        else "NON VALIDE — PROBLEMES A CORRIGER"
    )

    lines = [
        f"# RAPPORT DE SURVEILLANCE DEMO — {ts}",
        f"",
        f"## Verdict : {verdict}",
        f"**Score de validation : {checks_ok}/{checks_tot} ({pct}%)**",
        f"",
        f"## Metriques de performance",
        f"| Metrique | Valeur |",
        f"|---|---|",
        f"| Trades total | {stats['total_trades']} |",
        f"| Win rate | {stats['win_rate']*100:.1f}% |",
        f"| RR realise moyen | {stats['avg_rr']:.2f} |",
        f"| Expectancy par trade | {stats['expectancy']:.2f}$ |",
        f"| P&L session | {stats['session_pnl']:.2f}$ |",
        f"| Pertes consecutives | {stats['consecutive_losses']} |",
        f"",
        f"## Validation des 68% de completude",
        f"| Check | Statut | Valeur |",
        f"|---|---|---|",
    ]

    for check_name, ok, value in checks:
        icon = "OK" if ok else "FAIL"
        lines.append(f"| {check_name} | {icon} | {value} |")

    lines += [
        f"",
        f"## Qualite des confirmations d'entree",
        f"| Confirmation | Taux |",
        f"|---|---|",
        f"| MSS M1 confirme | {stats['mss_rate']*100:.0f}% |",
        f"| Entree en kill zone | {stats['kill_zone_rate']*100:.0f}% |",
        f"| H1 aligne | {stats['h1_aligned_rate']*100:.0f}% |",
        f"| Score confluence moyen | {stats['confluence_avg']:.1f} |",
        f"",
    ]

    if state["strategy_stats"]:
        lines += [
            f"## Performance par strategie",
            f"| Strategie | Wins | Losses | P&L |",
            f"|---|---|---|---|",
        ]
        for strat, s in state["strategy_stats"].items():
            lines.append(f"| {strat} | {s['wins']} | {s['losses']} | {s['pnl']:.2f}$ |")
        lines.append("")

    if state["alerts"]:
        lines += [f"## Alertes actives ({len(state['alerts'])})", "```"]
        lines += state["alerts"][-10:]
        lines += ["```", ""]

    lines += ["## Recommandations automatiques"]
    reco_count = 0

    if stats["win_rate"] < 0.45:
        lines.append(f"FAIL Win rate {stats['win_rate']*100:.1f}% trop bas -> verifier qualite des signaux d'entree")
        reco_count += 1

    if stats["avg_rr"] < 1.2:
        lines.append(f"FAIL RR realise {stats['avg_rr']:.2f} trop bas -> F-07 QuickExitManager toujours actif ?")
        reco_count += 1

    if stats["mss_rate"] < 0.5:
        lines.append(f"FAIL MSS confirme seulement {stats['mss_rate']*100:.0f}% -> amelioration MSS M1 pas active ?")
        reco_count += 1

    if stats["kill_zone_rate"] < 0.6:
        lines.append(f"FAIL Seulement {stats['kill_zone_rate']*100:.0f}% des trades en kill zone -> amelioration kill zones pas active ?")
        reco_count += 1

    if stats["consecutive_losses"] >= 3:
        lines.append(f"ALERTE {stats['consecutive_losses']} pertes consecutives -> analyser les derniers trades manuellement")
        reco_count += 1

    if reco_count == 0:
        lines.append("OK Aucune recommandation urgente — continuer la surveillance")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log(f"Rapport mis a jour -> {REPORT_FILE} | Score: {pct}% | {verdict}")

def read_new_events():
    """Lit les nouvelles lignes du fichier d'evenements."""
    if not os.path.exists(EVENTS_FILE):
        return []

    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = lines[state["last_line_count"]:]
    state["last_line_count"] = len(lines)
    return new_lines

def process_event(event):
    """Route un evenement vers l'analyseur approprie."""
    if not event:
        return

    etype = event.get("type", event.get("event_type", ""))

    if etype in ("DEMO_ORDER", "DEMO_ROUTER_ORDER_SENT", "TRADE_ENTRY"):
        analyze_trade_entry(event)

    elif etype in ("DEMO_ORDER_CLOSED", "TRADE_CLOSE", "POSITION_CLOSED"):
        analyze_trade_close(event)

    elif etype == "LIVE_SNAPSHOT":
        pnl = float(event.get("total", 0))
        if pnl < -SEUIL_DRAWDOWN_MAX_USD:
            alert(
                f"LIVE_SNAPSHOT total={pnl:.2f}$ — drawdown critique",
                critical=True
            )

def main():
    log("=" * 55)
    log("HERMES DEMO MONITOR — DEMARRAGE")
    log(f"Fichier surveille : {EVENTS_FILE}")
    log(f"Rapport : {REPORT_FILE}")
    log(f"Alertes : {ALERT_FILE}")
    log("=" * 55)

    Path(ALERT_FILE).parent.mkdir(parents=True, exist_ok=True)
    open(ALERT_FILE, "w").close()

    cycle = 0
    while True:
        try:
            new_lines = read_new_events()
            for line in new_lines:
                event = parse_event(line)
                process_event(event)

            if cycle % 6 == 0:
                stats  = compute_stats()
                checks = validation_68pct(stats)
                write_report(stats, checks)

                if stats and stats.get("total_trades", 0) > 0:
                    checks_ok = sum(1 for _, ok, _ in checks if ok)
                    log(
                        f"Score validation 68% : {checks_ok}/{len(checks)} "
                        f"| Trades: {stats['total_trades']} "
                        f"| Win: {stats['win_rate']*100:.0f}% "
                        f"| RR: {stats['avg_rr']:.2f} "
                        f"| PnL: {stats['session_pnl']:.2f}$"
                    )

            cycle += 1
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log("Arret du moniteur.")
            break
        except Exception as e:
            log(f"Erreur monitor : {e}", level="ERROR")
            time.sleep(10)

if __name__ == "__main__":
    main()
