# HERMES Dashboard — accès mobile via Tailscale

mission/DASHBOARD.md étape Tailscale. Tailscale n'est **pas installé** sur
cette machine (vérifié le 2026-07-08) — l'installation et la connexion
`tailscale up` nécessitent une authentification interactive (navigateur,
compte Google/Microsoft/GitHub/email de SIMO) que Claude ne peut pas
effectuer à la place de l'utilisateur. Les étapes ci-dessous sont à exécuter
à la main, une seule fois.

## Pourquoi Tailscale (et pas autre chose)

Le dashboard (`app/dashboard_api/server.py`) n'écoute que sur
`127.0.0.1:8010` — invisible depuis internet et même depuis le réseau local
par défaut. Tailscale crée un réseau privé virtuel (mesh VPN) entre les
appareils de SIMO uniquement (ce PC + le téléphone) ; `tailscale serve`
expose le port 8010 **uniquement à l'intérieur de ce réseau privé**, jamais
publiquement. C'est la différence avec `tailscale funnel` (qui, lui,
expose sur internet) — **ne jamais utiliser funnel** pour ce dashboard.

## 1. Installer Tailscale sur ce PC

```powershell
winget install Tailscale.Tailscale
```

(ou téléchargement direct : https://tailscale.com/download/windows)

## 2. Connecter ce PC au tailnet

```powershell
tailscale up
```

Ceci ouvre une page de connexion dans le navigateur — se connecter avec le
compte que SIMO veut utiliser pour son tailnet personnel (Google, Microsoft,
GitHub ou email). Une fois connecté, noter le nom de la machine :

```powershell
tailscale status
```

La colonne de gauche donne un nom du type `desktop-xxxx` — c'est le nom
tailnet de ce PC (ex: `hermes-pc`).

## 3. Exposer UNIQUEMENT le port du dashboard sur le tailnet

```powershell
tailscale serve --bg 8010
```

`--bg` = tourne en arrière-plan en permanence (persiste après redémarrage
de la session Tailscale). Vérifier ensuite :

```powershell
tailscale serve status
```

Doit afficher `https://hermes-pc.<tailnet>.ts.net -> 127.0.0.1:8010` (le
nom exact du tailnet dépend du compte choisi à l'étape 2).

**Ne jamais lancer `tailscale funnel`** sur ce port — funnel expose sur
internet public, à l'opposé de l'objectif de ce dashboard.

## 4. Installer Tailscale sur le téléphone

- App Store (iPhone) ou Play Store (Android) : rechercher "Tailscale"
- Se connecter avec le **même compte** qu'à l'étape 2
- Une fois connecté, ouvrir le navigateur du téléphone et aller sur
  l'adresse affichée par `tailscale serve status` (ex :
  `https://hermes-pc.<tailnet>.ts.net`)
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
