# MISSION : DASHBOARD COMPACT — AUDIT D'ABORD, PUIS REFONTE (UI SEULE)

Objectif : (1) **vérifier exactement ce qui existe** dans le dashboard actuel, puis
(2) le refondre pour qu'il ne soit plus « très très long », selon la maquette validée
(cockpit compact + commandes rangées du moins au plus dangereux). **Présentation/UI
uniquement.** On ne touche NI le moteur de trading NI le dataset.

═══════════════════════════════════════════════════════════════
INVARIANTS ABSOLUS
═══════════════════════════════════════════════════════════════
1. **ZÉRO modif du moteur de trading**, de la logique de décision, du writer
   `DECISION_DATASET`, d'aucun seuil. La mission ne touche QUE la couche
   dashboard/présentation (l'app FastAPI du dashboard sur le port 8010 + son HTML/JS/CSS).
2. Si le dashboard **partage des modules** avec le cœur de trading, NE TOUCHE PAS la
   logique partagée — restructure seulement la présentation.
3. Le **bot de trading continue de tourner**. Recharger/redémarrer le PROCESS DASHBOARD
   pour appliquer l'UI = OK (annonce-le). Ne JAMAIS arrêter ni redémarrer le bot de trading.
4. **Gel intact** : on reste en demo, bot en marche, le dataset continue d'accumuler.
   C'est un changement d'écran, pas de comportement.
5. **Sécurité mode réel** : NE réduis AUCUNE protection existante sur la bascule
   demo→réel, et N'ACTIVE / NE FACILITE PAS le trading live. Si la bascule réel n'est pas
   déjà pleinement câblée/protégée, LAISSE-LA telle quelle (juste restylée) et signale-le.
   N'écris AUCUN code qui rendrait le passage live plus facile. En cas de doute sur du code
   qui activerait le live → **STOP + rapport**, pas d'autonomie.
6. Safepoint git avant. Suite tests **VERTE** après (on est à 3373 passed / 0 failed — ça
   doit le rester). Rouge = rollback immédiat + STOP.

═══════════════════════════════════════════════════════════════
PART A — AUDIT (LECTURE SEULE, AVANT TOUTE ÉCRITURE)
═══════════════════════════════════════════════════════════════
Localise et cartographie le dashboard :
- Le(s) fichier(s) : app FastAPI (routes) + template(s) HTML / JS / CSS servis (port 8010).
- **Inventaire des ENDPOINTS existants** (noms + signatures EXACTS — ne devine pas) :
  - toggle symboles (gold# / btc# on/off)
  - contrôle bot : start / stop / restart
  - bascule mode demo ↔ réel
  - vérification PIN (PBKDF2)
  - status / données (`/api/status` et autres) → **quels CHAMPS sont réellement dispo**
    pour l'affichage (équité, P&L jour, kill-switch, EES vente/achat, session, news, DXY,
    régime ATR, positions, santé système…).
- **Structure HTML actuelle** : quelles sections, et surtout **ce qui rend la page
  "très très long"** — confirme : table système à N lignes toujours dépliée ? log
  d'événements sans limite ? les deux ? autre chose ?
- Mécanisme d'auth PIN actuel + **comment le mode réel est protégé aujourd'hui**.

**Livrable A : `DASHBOARD_AUDIT.md`** — endpoints exacts, champs `/api/status` réellement
disponibles, structure actuelle, les coupables de longueur confirmés, état de protection
du mode réel.

**Point de contrôle après l'audit** : si l'audit révèle (a) que le dashboard partage du
code avec le moteur, (b) que la bascule réel serait activable sans protection suffisante,
ou (c) tout autre risque de toucher au trading → SIGNALE-le et reste en périmètre
présentation strict pour la Part B.

═══════════════════════════════════════════════════════════════
PART B — REFONTE (COUCHE DASHBOARD/PRÉSENTATION UNIQUEMENT)
═══════════════════════════════════════════════════════════════
Restructure le HTML/CSS/JS du dashboard selon la maquette validée. Garde le **thème SOMBRE
actuel**, mobile-first, une seule page **sans scroll interminable**. N'affiche que les
champs qui existent réellement (selon l'audit) ; câble les commandes aux endpoints trouvés.

**Monitoring (haut) :**
- Équité + P&L du jour (2 gros chiffres, P&L coloré vert/rouge).
- Bandeau kill-switch (état + heure de reprise + nb positions).
- Capteurs en **pastilles compactes 2 colonnes** (EES vente/achat avec bande
  sain/prudence/extrême, session, news, DXY, régime ATR — selon champs dispo).
- Activité : **plafonnée aux ~4-5 dernières lignes** ← FIX LONGUEUR n°1.
- Système : la longue table → **une rangée de puces** (bot / watchdog / git / tests /
  médecin), vert = OK, détail affiché seulement si un point est rouge ← FIX LONGUEUR n°2.

**Commandes (rangées du MOINS au PLUS dangereux) :**
- **Symboles** (risque faible) : toggles gold# / btc# → câblés à l'endpoint toggle existant.
- **Robot** (risque moyen) : start / stop / restart, avec **confirmation obligatoire** sur
  stop ET restart (anti-faux-doigt mobile) → câblés aux endpoints existants.
- **Mode** (le plus dangereux) : demo actif / réel **verrouillé PIN**, isolé, style danger,
  protection **au moins égale** à l'existant → câblé à la bascule + vérif PIN existantes.

Câble TOUTES les commandes aux endpoints réels de la Part A. N'invente AUCUNE nouvelle
logique backend de trading. Si un endpoint manque, utilise ce qui existe et signale le
manque — ne fabrique pas de contrôle live.

═══════════════════════════════════════════════════════════════
VÉRIFICATION
═══════════════════════════════════════════════════════════════
- `git diff --stat` : ne touche QUE les fichiers dashboard/présentation.
  **Zéro ligne dans le moteur / la logique de décision / le writer dataset.** Montre le diff.
- Suite complète : **0 failed** (toujours 3373 vert).
- Le bot de trading n'a été ni arrêté ni redémarré (seul le process dashboard a pu recharger).
- `DECISION_DATASET` non touché (diff vide côté dataset).

**Livrable B : `DASHBOARD_REDESIGN_REPORT.md`** — ce qui a changé, preuve `git diff = dashboard
only`, tests verts, confirmation que les 2 coupables de longueur sont réglés, que les
commandes sont rangées par danger, et que les protections du mode réel sont intactes.

═══════════════════════════════════════════════════════════════
CLÔTURE
═══════════════════════════════════════════════════════════════
Affiche dans le terminal : périmètre respecté (UI only), tests verts, bot de trading intact,
gel intact, dataset non touché. Rappelle l'URL Tailscale pour que SIMO recharge et voie le
nouveau cockpit.

Rappel : cette mission change l'**écran**, pas le **comportement**. Demo reste demo, le bot
reste en marche, le dataset continue. Les boutons « arrêter » et « passer en réel » sont
là par design — mais rien dans cette mission ne les déclenche.
