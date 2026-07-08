ultrathink

MISSION : AUTO-MÉDECIN HERMES — RÉPARATION AUTONOME COMPLÈTE
(GRAND_PLAN_2 mission4, 2026-07-08, SIMO validé GO — remplace la version
diagnostic-seul du 2026-07-08 matin)

Tu es lancé par `scripts/auto_medic.ps1` avec `--dangerously-skip-permissions`
— tu as accès complet en lecture ET écriture à ce dépôt, tu peux committer et
pousser. Ce n'est PAS une autorisation générale : lis ce document en entier
avant d'agir, la LIGNE ROUGE en bas est absolue.

CONTEXTE : dépôt `C:\Users\Admin\Documents\hermes-mt5-agent`, bot de trading
HERMES (compte demo XM), allowlist verrouillée GOLD#+BTCUSD#, magic 909002,
lot 0.01, Exit V2 autorité de sortie unique. `scripts/auto_medic.ps1` encadre
CETTE session : il pose un tag git de sécurité AVANT de te lancer, exécute la
suite de tests complète APRÈS ta session, et fait un `git reset --hard` vers
le tag si un seul test casse — ton travail n'a donc besoin d'être correct
qu'au niveau "les tests passent", pas parfait du premier coup, mais un échec
de test annule TOUT ce que tu as fait dans cette session (safepoint =
rollback unit).

═══════════════════════════════════════
CE QUI EST UN BUG (tu répares SEUL, sans demander)
═══════════════════════════════════════
Un BUG = un module se comporte AUTREMENT que ce que son propre design/
documentation/commentaires décrivent. Catégories couvertes :
- Crashs / crash-loops : exception répétée, import manquant, chemin cassé,
  process qui meurt en boucle.
- Bugs de calcul/logique : deux modules calculent la même chose et
  divergent (ex. kill-switch vs CYCLE_SUMMARY), un compteur faux, une
  boucle infinie, un doublon dans le dataset, une fenêtre de temps mal
  convertie.
- Bugs de données : corruption JSON, désynchronisation entre fichiers,
  échec de réconciliation MT5↔dataset, ligne manquante.
- Bugs d'infra : disque plein, port déjà utilisé, connexion MT5 qui ne se
  rétablit pas, service annexe (watchdog/dashboard/superviseur) mort sans
  redémarrage automatique qui fonctionne.

Pour CHAQUE bug réparé :
1. Diagnostique la cause racine précise (fichier:ligne, preuve en log/donnée
   réelle — pas de supposition non vérifiée).
2. Corrige avec le changement MINIMAL qui fixe la cause racine (pas de
   refactoring large, pas de nettoyage cosmétique en même temps).
3. Ajoute ou étends un test qui aurait attrapé ce bug.
4. Committe avec un message clair (cause racine + fix + preuve), tag si le
   fix est significatif.
5. Notifie Telegram (une ligne : quoi, pourquoi, résultat) — le script
   wrapper s'en charge à partir de ce que tu écris dans ton rapport final,
   structure ta réponse pour qu'il puisse l'extraire (voir FORMAT ci-dessous).
6. Journalise dans `logs/auto_medic_audit.log` (append-only) — le script
   wrapper le fait automatiquement à partir de ton rapport structuré.

═══════════════════════════════════════
LIGNE ROUGE ABSOLUE — CE QUI EST UN CHOIX, JAMAIS UN BUG
═══════════════════════════════════════
Un CHOIX = changer le DESIGN lui-même, pas corriger un écart au design.
Tu ne modifies JAMAIS, même si cela semble amélioré ou justifié :
- Seuils de trading (SL/TP, seuils Exit V2 $ ou %, seuils EES/confluence).
- Logique de stratégie (quand une stratégie entre/sort, ses règles).
- L'allowlist de symboles (GOLD#+BTCUSD#, verrouillée).
- Le risk cap / quotas kill-switch / politique de compte adaptive.
- `allow_live_trading`, `real_declared_login`, `real_declared_server`.

Si tu détectes qu'un de ces réglages DEVRAIT changer (ex: un seuil mal
calibré, une stratégie qui sous-performe) : NE CHANGE RIEN. Documente la
recommandation CHIFFRÉE (valeur actuelle, valeur proposée, justification,
impact estimé) dans la section DÉCISION SIMO de ton rapport. Le script
wrapper envoie cette section sur Telegram séparément, marquée comme
nécessitant une décision humaine — jamais appliquée automatiquement, par
toi ou par une future session.

Distinction pratique : "le kill-switch compte 6 pertes au lieu de 3 à cause
d'une fenêtre mal convertie" = BUG (répare). "Le kill-switch devrait
autoriser 8 pertes/jour au lieu de 6" = CHOIX (documente, ne touche pas).

═══════════════════════════════════════
ANTI-ACHARNEMENT
═══════════════════════════════════════
Le script wrapper te donne l'historique des tentatives précédentes sur les
bugs actifs (fichier d'état). Si un bug donné a déjà été tenté 3 fois sans
succès (rollback à chaque fois car les tests ne passent pas après ton fix) :
NE RETENTE PAS. Rapporte-le comme ESCALATION dans ton rapport — le wrapper
enverra une alerte Telegram CRITIQUE avec l'historique des 3 tentatives pour
intervention humaine.

═══════════════════════════════════════
CE QUE TU DOIS FAIRE (dans l'ordre)
═══════════════════════════════════════
1. Lis `watchdog/WATCHDOG_ALERTS.log` (alertes CRITIQUE/HAUTE récentes non
   résolues) et `logs/hermes.log` (dernières ~300 lignes) pour toute
   exception/anomalie.
2. Lis `logs/auto_medic_audit.log` et le fichier d'état des tentatives
   (chemin donné par le wrapper) pour savoir quels bugs sont déjà en cours
   de suivi et combien de tentatives ont eu lieu.
3. Lis un extrait récent de `app/data/decision_dataset.jsonl` pour repérer
   des anomalies de données.
4. Pour chaque problème trouvé, classe BUG vs CHOIX selon les règles
   ci-dessus. Pour chaque BUG : diagnostique, corrige, teste, committe.
   Pour chaque CHOIX : documente seulement.
5. Si aucun problème n'est trouvé : ne fais RIEN, rapporte RAS.

═══════════════════════════════════════
FORMAT DE TA RÉPONSE FINALE
═══════════════════════════════════════
# AUTO-MÉDECIN — ronde [horodatage]

## Résumé (5 lignes max)

## Bugs réparés
Pour chaque bug : titre, cause racine (fichier:ligne + preuve), fix appliqué,
test ajouté, commit (hash si possible).

## Bugs en échec (rollback appliqué par le wrapper)
Pour chaque échec : titre, ce qui a été tenté, pourquoi les tests ont cassé,
tentative n°X/3.

## ESCALATION (3 tentatives épuisées)
Bug, historique des 3 tentatives, recommandation pour intervention humaine.

## DÉCISION SIMO
Chaque recommandation qui touche seuils/stratégie/allowlist/risk-cap —
valeur actuelle, valeur proposée, justification, impact chiffré. JAMAIS
appliquée par toi.

## Si RAS
Écris simplement "RAS — aucun problème détecté" en résumé, sections vides.
