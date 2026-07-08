# HERMES Dashboard — accès mobile via Tailscale

## État actuel (2026-07-08, mis à jour)

- Tailscale **installé et connecté** sur ce PC : `100.74.170.107`
  (device `6a4c09c70e4590e`, compte `med.business50@...`).
- iPhone de SIMO **déjà sur le même tailnet** : `100.78.150.18`
  (`iphone-13-pro-max`) — l'app Tailscale est déjà installée côté téléphone.
- `app/dashboard_api/server.py` **tourne en permanence** (process
  autonome, port `127.0.0.1:8010` uniquement — vérifié via `netstat`,
  jamais `0.0.0.0`), supervisé par la tâche planifiée
  `HERMES_DASHBOARD_SUPERVISOR` (déclenchée au boot, créée le 2026-07-08,
  même anti-crash-loop que `HERMES_BOT_SUPERVISOR`).
- PIN déjà généré au premier démarrage : voir `logs/hermes.log`, ligne
  `[DASHBOARD] PREMIER DEMARRAGE — PIN genere: ...` (une seule fois,
  changez-le si besoin via `dashboard/.env`, voir en bas de page).
- **Bloquant restant : `tailscale serve` doit être activé une fois pour le
  tailnet**, une action gated derrière la connexion Tailscale de SIMO que
  Claude ne peut pas effectuer à sa place (voir étape 3 ci-dessous —
  c'est la SEULE étape manuelle qui reste).

## Pourquoi pas `0.0.0.0`

Un changement a été demandé le 2026-07-08 pour faire écouter le dashboard
sur `0.0.0.0` (toutes interfaces) afin de le rendre joignable via
Tailscale. Refusé et remplacé par `tailscale serve` pour deux raisons :
1. Le port cité (8000) est en fait `app/local_api/server.py` — un service
   **sans PIN ni authentification du tout** (lecture seule, mais aucune
   protection). L'exposer changerait complètement son modèle de sécurité.
2. Même pour le vrai dashboard PIN-protégé (8010) : le profil pare-feu
   Windows "Privé" couvre en général aussi le Wi-Fi domestique, pas
   seulement l'interface virtuelle Tailscale — `0.0.0.0` aurait donc pu
   exposer le dashboard à tout le réseau maison, pas seulement au tailnet.
   `tailscale serve` évite ce risque par construction : c'est le démon
   Tailscale lui-même qui fait proxy vers `127.0.0.1:8010`, le bind
   d'écoute de l'app ne change jamais.

## Pourquoi Tailscale (et pas autre chose)

Le dashboard (`app/dashboard_api/server.py`) n'écoute que sur
`127.0.0.1:8010` — invisible depuis internet et même depuis le réseau local
par défaut. Tailscale crée un réseau privé virtuel (mesh VPN) entre les
appareils de SIMO uniquement (ce PC + le téléphone) ; `tailscale serve`
expose le port 8010 **uniquement à l'intérieur de ce réseau privé**, jamais
publiquement. C'est la différence avec `tailscale funnel` (qui, lui,
expose sur internet) — **ne jamais utiliser funnel** pour ce dashboard.

## 1. Installer Tailscale sur ce PC — FAIT

Déjà installé et connecté (`100.74.170.107`).

## 2. Connecter ce PC au tailnet — FAIT

```powershell
tailscale status
```
```
100.74.170.107  6a4c09c70e4590e    med.business50@  windows  -
100.78.150.18   iphone-13-pro-max  med.business50@  iOS      -
```
Les deux appareils (PC + iPhone de SIMO) sont déjà sur le même tailnet.

## 3. Exposer UNIQUEMENT le port du dashboard sur le tailnet — ACTION MANUELLE REQUISE

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" serve --bg 8010
```

Tenté le 2026-07-08 : refusé avec le message
```
Serve is not enabled on your tailnet.
To enable, visit:
         https://login.tailscale.com/f/serve?node=ng1HJ6ypft11CNTRL
```

**C'est la seule étape manuelle qui reste.** `Serve` doit être activé une
fois pour l'ensemble du tailnet, une action liée au compte Tailscale de
SIMO (login requis) que Claude ne peut pas faire à sa place. Une fois
visité et activé :

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" serve --bg 8010
& "C:\Program Files\Tailscale\tailscale.exe" serve status
```

`--bg` = tourne en arrière-plan en permanence (persiste après redémarrage
de la session Tailscale). `serve status` doit afficher quelque chose comme
`https://6a4c09c70e4590e.<tailnet>.ts.net -> 127.0.0.1:8010` (le nom exact
dépend du tailnet du compte `med.business50@...`).

**Ne jamais lancer `tailscale funnel`** sur ce port — funnel expose sur
internet public, à l'opposé de l'objectif de ce dashboard.

## 4. Téléphone de SIMO — FAIT (app déjà installée, déjà sur le tailnet)

Une fois l'étape 3 activée :
- Ouvrir le navigateur du téléphone et aller sur l'URL affichée par
  `tailscale serve status` (ex : `https://6a4c09c70e4590e.<tailnet>.ts.net`)
- Ajouter cette page à l'écran d'accueil pour un accès en un tap

## 5. Preuve de non-exposition publique

Depuis ce PC, une fois `tailscale serve` actif :

```powershell
netstat -ano | findstr :8010
```

Doit montrer **uniquement** `127.0.0.1:8010` (jamais `0.0.0.0:8010`) —
aucun listener public sur ce port. Le trafic Tailscale lui-même passe par
l'interface réseau virtuelle Tailscale (`100.x.x.x`), jamais par une IP
publique. Depuis un réseau externe (4G du téléphone sans Tailscale actif),
tenter l'URL `.ts.net` doit échouer — c'est la preuve que rien n'est
public.

## Après connexion

Le PIN du dashboard est généré automatiquement au premier démarrage de
`python -m app.dashboard_api.server` et affiché **une seule fois** dans les
logs (`logs/hermes.log`, ligne `[DASHBOARD] PREMIER DEMARRAGE — PIN
genere: XXXXXX`). Le noter immédiatement — il ne sera plus jamais réaffiché
en clair. Pour le changer, éditer `dashboard/.env` (supprimer
`DASHBOARD_PIN_HASH`/`DASHBOARD_PIN_SALT`, relancer le serveur pour en
générer un nouveau).
