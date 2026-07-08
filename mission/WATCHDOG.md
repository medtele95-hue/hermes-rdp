ultrathink

MISSION : HERMES WATCHDOG — CHIEN DE GARDE INDÉPENDANT (surveillance + alertes, jamais de trading)

Objectif SIMO : un surveillant INDÉPENDANT du bot qui vérifie en permanence que la réalité (MT5 + fichiers) respecte les invariants, et qui alerte immédiatement en cas de violation. Motivé par les incidents réels : trades BTC illégitimes, bot tournant sans logs, invariants perdus à la reconstruction.

Safepoint git avant. Tests après.

ARCHITECTURE :
- Script séparé watchdog/hermes_watchdog.py — PROCESS INDÉPENDANT du bot (survit à un crash du bot, le surveille de l'extérieur). Boucle toutes les 60 secondes. Connexion MT5 en LECTURE SEULE (positions_get, history_deals_get, account_info) + lecture des fichiers (dataset, logs, backups). AUCUN order_send, AUCUNE modification de fichier du bot — à UNE exception près (voir MODE URGENCE).
- Lancement : script de démarrage + tâche planifiée Windows (démarre au boot de la machine, redémarre s'il crash). Le watchdog doit être encore plus fiable que le bot.

LES VÉRIFICATIONS (chaque cycle) :
1. SYMBOLES : toute position/deal du jour sur un symbole hors GOLD# → ALERTE CRITIQUE.
2. MAX_OPEN : plus d'1 position GOLD# simultanée → ALERTE CRITIQUE.
3. IDENTITÉ : position avec lot ≠ 0.01 ou magic ≠ 909002 → ALERTE CRITIQUE (position inconnue sur le compte).
4. RISQUE NU : position sans SL ou sans TP → ALERTE CRITIQUE.
5. PERTE FLOTTANTE : floating loss totale > 3% équité → ALERTE HAUTE.
6. HEARTBEAT BOT : app/data/decision_dataset.jsonl (et le log s'il existe) non modifié depuis > 10 minutes pendant les heures de marché → ALERTE HAUTE "bot possiblement mort".
7. KILL-SWITCH INDÉPENDANT : recompte les pertes du jour broker depuis history_deals_get ; si > quota de la politique active ET que de nouveaux trades s'ouvrent → ALERTE CRITIQUE "kill-switch percé".
8. SANTÉ MACHINE : disque < 5 GB libre → ALERTE ; dossier backups/ sans sous-dossier daté du jour après 10h → ALERTE "backup manquant".
9. DOUBLE INSTANCE : plus d'un process python exécutant le bot → ALERTE CRITIQUE.

ALERTES :
- Fichier WATCHDOG_ALERTS.log (append, horodaté, niveau CRITIQUE/HAUTE/INFO) + affichage console.
- TELEGRAM : notification push via bot Telegram (python-telegram-bot ou simple requête HTTPS à l'API). Créer le guide pas-à-pas pour SIMO : créer le bot via @BotFather, récupérer le token et le chat_id, les mettre dans watchdog/.env. Fail-safe : Telegram indisponible → l'alerte vit quand même dans le fichier, le watchdog continue.
- Anti-spam : une même alerte non résolue ne se renvoie que toutes les 30 minutes.

MODE URGENCE (une seule action autorisée, configurable) :
- WATCHDOG_EMERGENCY_CLOSE=false par défaut. SI SIMO l'active : sur ALERTE CRITIQUE de type symbole interdit (vérification 1) UNIQUEMENT, le watchdog peut fermer cette position illégitime via un order_send de clôture dédié, loggé [WATCHDOG_FORCE_CLOSE]. Tout le reste : alerte seulement, jamais d'action. Documenter clairement le flag pour que SIMO choisisse.

HEARTBEAT DU WATCHDOG LUI-MÊME :
- Il écrit watchdog/heartbeat.txt (timestamp) chaque cycle — pour qu'on puisse vérifier que le gardien lui-même est vivant.

TESTS : simuler chaque violation (position BTC fictive en dry-run, dataset gelé, etc.) → l'alerte part ; Telegram mort → fichier quand même ; le watchdog ne peut PAS envoyer d'ordre hors du mode urgence (test qui le prouve).

LIVRABLE : WATCHDOG_REPORT.md — architecture, les 9 vérifications testées, guide Telegram pas-à-pas pour SIMO, commande de lancement + tâche planifiée installée, et l'état de la première ronde réelle (qu'a-t-il vu sur le compte maintenant ?).
