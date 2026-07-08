ultrathink

URGENT : BUG DATE KILL-SWITCH — LA FENÊTRE POINTE SUR LE MAUVAIS MOIS (4e bug timezone de cette famille)

Constat prouvé par les logs du 2026-07-08 à 11:34 :
[DAILY_KILLSWITCH] triggered=False losses=0/6 policy=DEMO window=[2026-05-31T21:00:00+00:00 -> 2026-06-01T12:00:00+00:00]

Le bot tourne le 8 JUILLET mais le kill-switch calcule sa fenêtre jour-broker sur le 31 MAI → 1er JUIN — décalage de plus d'un mois. CONSÉQUENCE CRITIQUE : le kill-switch compte les pertes dans une fenêtre historique morte, ne voit AUCUNE perte du jour réel (losses=0/6 en permanence), et NE PEUT DONC JAMAIS DÉCLENCHER. Le garde-fou principal est aveugle — même classe de faille que le bug des 3h de la semaine dernière, mais pire (décalage d'un mois).

Safepoint git avant. Tests après. C'est la priorité absolue — un kill-switch aveugle sur un système qui trade est un risque direct.

DIAGNOSTIC :
1. Trouve le calcul de la fenêtre jour-broker du kill-switch. D'où vient la date de base ? Suspects :
   - Une date "now" figée/cachée quelque part (une variable calculée une seule fois au boot et jamais rafraîchie ?)
   - Le décalage serveur broker (UTC+3) mal appliqué qui projette la date dans le passé
   - Une lecture de l'heure broker via un tick/deal ancien au lieu de l'heure courante
   - Un mélange entre la date du premier deal de l'historique et la date du jour
2. Pourquoi précisément le 31 mai → 1er juin ? Cette date correspond-elle à quelque chose (premier deal du compte ? une constante ? un seed de test resté en dur ?) — trace l'origine exacte.

FIX :
- La fenêtre jour-broker DOIT être calculée à partir de l'heure COURANTE réelle (datetime.now en UTC, convertie explicitement en heure broker UTC+3), recalculée à CHAQUE évaluation du kill-switch — jamais figée, jamais dérivée d'un deal ancien.
- Fenêtre = [minuit broker du jour courant, now+marge]. Le reset reste 21:00 UTC (= minuit serveur UTC+3).
- Le compteur de pertes relit l'historique deals broker DANS cette fenêtre corrigée.
- Log de contrôle : afficher la date courante détectée ET la fenêtre calculée côte à côte, pour qu'une dérive future soit visible immédiatement.

TESTS D'INVARIANT (renforcer la famille timezone) :
- Simuler now = aujourd'hui → la fenêtre couvre aujourd'hui, pas un mois passé.
- Simuler 3 pertes réelles aujourd'hui → losses=3/6 correctement compté (pas 0).
- Simuler now à différentes heures (avant/après 21:00 UTC) → la fenêtre bascule au bon moment.
- Test anti-régression : la fenêtre ne doit JAMAIS pointer sur une date antérieure à aujourd'hui moins 48h, quelle que soit la source d'heure. Ajouter une assertion de garde-fou qui logue une ALERTE CRITIQUE si la fenêtre calculée est vieille de plus de 48h (détection automatique de ce type de bug à l'avenir).

VÉRIFIER AUSSI : ce même bug de date affecte-t-il d'AUTRES modules qui utilisent une fenêtre jour-broker ? (le flat-weekend, le blackout post-weekend, le bilan quotidien, l'outcome tracker du dataset). Auditer chaque calcul de "jour courant" dans le codebase — si la même source d'heure figée est utilisée ailleurs, corriger partout. C'est le 4e bug de cette famille : il faut une source d'heure broker UNIQUE, centralisée, testée, utilisée par tous.

LIVRABLE : origine exacte de la date figée (31 mai), le fix, la source d'heure centralisée, les autres modules affectés et corrigés, et la preuve par log que le kill-switch voit maintenant le bon jour (window = 8 juillet).

═══════════════════════════════════════
VOLET 2 (même mission) — AUDIT DU COMPORTEMENT BTC (scalp haute fréquence suspect)
═══════════════════════════════════════
Constat SIMO (historique MT5 du 08/07) : 6 trades BTCUSD# entre 04h38 et 05h36, tous SELL, tenus 2-12 minutes, gains minuscules (+0.11, +0.13, +0.16, +0.34, +0.71) et un perdant (-2.45). Profil = scalp haute fréquence à micro-gains — ressemble au penny-grab qu'on a tué sur GOLD.

DIAGNOSTIC (read-only) :
1. Ces 6 trades BTC : quel chemin de sortie les a fermés ? Exit V2 (BE/trailing) ou un autre closer ? Vérifie que process_gold_exit_v2 (ou son équivalent) s'applique BIEN à BTCUSD# et pas seulement à GOLD#. Si BTC est fermé par un vieux quick-exit money-based résiduel → c'est le penny-grab qui a survécu sur BTC.
2. Exit V2 est-il actif sur BTC avec les mêmes paramètres que GOLD (BE +2$, trailing) ? Rappel recherche : les stops BTC devraient être en % du prix (BTC ~63000, un mouvement de 0.11$ est infinitésimal) — les seuils $ absolus de GOLD (BE +2$) sont peut-être inadaptés à l'échelle BTC.
3. Ces micro-gains BTC : le TP money capper (max_tp_usd) qu'on a désactivé sur GOLD — est-il ENCORE actif sur BTC ? (GOLD était hard-exclu, mais BTC ?)
4. La fréquence : 6 trades BTC en 1h = proche du profil "sur-trading" que la recherche identifie comme fragile. Est-ce le comportement voulu ou une stratégie BTC trop laxiste ?

DÉCISION (RÉSERVÉ SIMO — ne pas modifier le trading BTC sans son accord, juste diagnostiquer et proposer) :
- Documenter précisément ce qui fait trader BTC ainsi.
- Proposer (chiffré, sans appliquer) : soit adapter Exit V2 à l'échelle BTC (seuils en % du prix), soit resserrer les gates BTC, soit désactiver temporairement BTC en attendant sa calibration propre — SIMO tranchera.
- Le dataset doit bien taguer ces trades BTC (symbol=BTCUSD#) pour mesurer leur edge réel séparément.

LIVRABLE VOLET 2 : qui ferme les trades BTC (Exit V2 ou résidu), si les paramètres sont à l'échelle BTC, et une proposition chiffrée pour SIMO (adapter/resserrer/suspendre BTC).
