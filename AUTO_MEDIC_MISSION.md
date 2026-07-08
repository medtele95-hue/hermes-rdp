ultrathink

MISSION : AUTO-MÉDECIN HERMES — DIAGNOSTIC SEUL (aucune écriture, aucune modification)

Tu es lancé en mode strictement LECTURE SEULE (outils disponibles : Read, Grep, Glob
uniquement — Write/Edit/Bash/WebFetch sont explicitement bloqués par la configuration
de lancement, pas par cette instruction). Tu NE PEUX PAS et tu NE DOIS PAS tenter de
modifier, créer, ou supprimer un fichier, ni exécuter une commande, ni committer, ni
pousser quoi que ce soit — même si cela semblerait utile. Si un outil est refusé,
n'essaie pas de contourner avec un autre outil : documente-le comme une action
recommandée dans ta réponse, pour un humain ou une future session Claude Code avec
les permissions normales.

CONTEXTE : dépôt `C:\Users\Admin\Documents\hermes-mt5-agent`, bot de trading HERMES
(compte demo XM), allowlist verrouillée GOLD#+BTCUSD#, magic 909002, lot 0.01.

CE QUE TU DOIS FAIRE (dans l'ordre) :
1. Lis `watchdog/WATCHDOG_ALERTS.log` (les alertes CRITIQUE/HAUTE non résolues récentes).
2. Lis les derniers logs du bot (`logs/hermes.log`, dernières ~200 lignes) — cherche des
   exceptions répétées, des boucles de logs, des symptômes de crash.
3. Lis l'état git (derniers commits/tags via `git log`/`git status` NE SONT PAS
   accessibles — Bash est bloqué ; base-toi sur ce que tu peux lire dans les fichiers
   du dépôt : `HERMES_STATE.md` à la racine contient déjà un résumé git à jour, utilise-le).
4. Lis un extrait récent de `app/data/decision_dataset.jsonl` (dernières lignes) pour
   repérer des anomalies (dataset figé, erreurs répétées, symbole interdit apparu).
5. Diagnostique chaque problème trouvé et classe-le :
   (a) bot mort/crash-loop — identifie la cause probable dans les logs (import manquant,
       chemin, exception répétée) ;
   (b) boucles de logs / spam — identifie la source ;
   (c) invariant percé (symbole interdit, lot ≠ 0.01, magic ≠ 909002, SL/TP manquant) —
       signale-le en CRITIQUE ;
   (d) disque plein — signale, propose quels fichiers purger (jamais le dataset vivant) ;
   (e) fichier corrompu non-critique — signale, propose une réparation.
6. Pour CHAQUE problème diagnostiqué, propose une correction CONCRÈTE et CHIFFRÉE
   (fichier:ligne si possible, diff proposé en texte) — mais NE L'APPLIQUE PAS. Ce sont
   des recommandations pour la section RÉSERVÉ SIMO.

TU N'ES PAS AUTORISÉ (et de toute façon tu n'as pas les outils) À : modifier des
stratégies, seuils, gates, politique de risque, quotas kill-switch, ou tout paramètre
de trading — même en recommandation, marque-les explicitement RÉSERVÉ SIMO plutôt que
« recommandé d'appliquer automatiquement ».

FORMAT DE TA RÉPONSE FINALE (c'est TOUT ce qui sera capturé — ta réponse texte finale
devient intégralement le rapport, écris-la donc directement au format Markdown final) :

# AUTO-MÉDECIN — diagnostic [horodatage que tu déduis du contexte disponible]

## Résumé (5 lignes max)
...

## Problèmes détectés
Pour chaque problème : titre, catégorie (a-e ci-dessus ou "aucun"), preuve (citation
fichier:ligne ou log), gravité.

## Section RÉSERVÉ SIMO
Chaque recommandation de correction, avec la modification exacte proposée (jamais
appliquée), et si elle touche une stratégie/un seuil/une politique de risque —
explicitement marqué comme tel.

## Si aucun problème trouvé
Écris simplement "RAS — aucun problème détecté" en résumé, ne remplis pas les autres
sections avec du bruit.
