# BACKUP HERMES — instructions pour SIMO (leçon de la panne du serveur)

Le serveur précédent est mort avec **une semaine de travail non sauvegardée**
(30 juin → 7 juillet 2026 : fixes critiques + dataset perdus). Ce dispositif
rend cette perte impossible à reproduire. **Trois étages, dans l'ordre.**

## Étage 1 — Remote git privé (À FAIRE UNE FOIS, 3 étapes)

Le repo local a tous les commits et tags, mais AUCUN remote. Sans remote,
une panne disque efface tout l'historique.

1. Créer un repo **privé** vide sur GitHub (ou GitLab) : nom suggéré
   `hermes-mt5-agent`, ne rien y initialiser (pas de README).
2. Dans PowerShell, dans `C:\Users\Admin\Documents\hermes-mt5-agent` :
   ```powershell
   C:\Tools\MinGit\cmd\git.exe remote add origin https://github.com/<TON_COMPTE>/hermes-mt5-agent.git
   C:\Tools\MinGit\cmd\git.exe push -u origin --all
   C:\Tools\MinGit\cmd\git.exe push origin --tags
   ```
   (GitHub demandera un login la première fois — utiliser un Personal
   Access Token comme mot de passe.)
3. Vérifier sur la page GitHub que les branches et les tags
   (`fix-mtf-structure-v1`, `exit-v2-activated`, `ks-policy-done`, …)
   sont bien visibles.

Une fois ce remote configuré, la tâche planifiée quotidienne pousse
automatiquement branches + tags chaque jour (`scripts/backup_daily.ps1`).

## Étage 2 — Copie quotidienne locale (AUTOMATIQUE)

La tâche planifiée Windows `HERMES_DAILY_BACKUP` exécute chaque jour à
21:30 locale `scripts/backup_daily.ps1`, qui copie :
- `app/data/*.jsonl` et `*.json` (dataset de décisions, events, caches),
- `.env`,
- `reports/`,
vers `backups/AAAA-MM-JJ/` (30 jours conservés), puis pousse git vers
`origin` si configuré. Journal : `backups/backup.log`.

Vérifier qu'elle existe : `schtasks /Query /TN HERMES_DAILY_BACKUP`.
La recréer si absente :
```powershell
schtasks /Create /F /SC DAILY /ST 21:30 /TN HERMES_DAILY_BACKUP /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Admin\Documents\hermes-mt5-agent\scripts\backup_daily.ps1"
```

## Étage 3 — Copie HORS MACHINE (MANUEL, 1×/semaine, 2 minutes)

Le git privé protège le code ; les copies datées protègent les données ;
mais si la machine meurt ET que le remote n'a pas été poussé, il faut un
troisième filet **hors de la machine** :

1. Ouvrir `C:\Users\Admin\Documents\hermes-mt5-agent\backups\`.
2. Glisser le dossier daté le plus récent (ex. `2026-07-07`) dans le
   Google Drive (dossier `HERMES_BACKUPS`).
3. C'est tout. Une fois par semaine minimum, ou après toute journée de
   trading importante.

## Note bridge (pour mémoire)

L'EA `HermesBridge` et l'endpoint `/api/mt5/poll-commands` mentionnés dans
les anciennes notes n'existent **nulle part** dans cette copie (vérifié le
2026-07-07 : repo, dossier MT5, MetaQuotes). C'était un résidu du
monitoring de l'ancien serveur → **ne pas réinstaller**. Le monitoring
local se fait via le dashboard `http://127.0.0.1:8000`.
