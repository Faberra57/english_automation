# Déploiement serveur avec Docker

Ce déploiement lance trois conteneurs :

- `english-teacher` : bot Telegram, DeepSeek, xAI, ElevenLabs et tâches planifiées ;
- `dashboard` : interface Streamlit sur `127.0.0.1:8501` ;
- `anki` : Anki Desktop dans une interface web sur `127.0.0.1:3000`, avec ses données persistantes.

Anki est le seul composant qui demande une configuration graphique lors du premier lancement.
Tout le reste est automatisé par `scripts/deploy-server.sh`.

## 1. Serveur nécessaire

Configuration conseillée : Ubuntu 24.04 LTS 64 bits, 2 CPU, 4 Go de RAM et au moins
10 Go de disque. Anki Desktop est le composant le plus lourd. Aucun domaine n'est
obligatoire : les interfaces restent privées et sont accessibles par tunnel SSH.

Installez seulement :

- Git ;
- Docker Engine ;
- les plugins Docker Buildx et Docker Compose.

Sur Ubuntu, utilisez le dépôt `apt` officiel de Docker, puis installez :

```bash
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Déconnectez-vous puis reconnectez-vous, et vérifiez :

```bash
docker version
docker compose version
```

## 2. Copier le projet

```bash
git clone <URL_DU_DEPOT_GITHUB> english_automation
cd english_automation
cp .env.example .env
chmod 600 .env
```

Dans `.env`, renseignez au minimum :

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=...
TELEGRAM_CHAT_ID=...
DEEPSEEK_API_KEY=...
XAI_API_KEY=...
ELEVENLABS_API_KEY=...

APP_UID=1000
APP_GID=1000
ANKI_ENABLED=false
```

Obtenez les valeurs UID/GID du compte Linux avec `id -u` et `id -g`. Gardez
`ANKI_ENABLED=false` pendant la première configuration d'Anki. Les clés déjà publiées
dans une conversation ou un dépôt doivent être révoquées et remplacées avant le déploiement.

## 3. Premier démarrage

```bash
./scripts/deploy-server.sh
```

Le script :

1. vérifie Docker et les variables obligatoires ;
2. crée les dossiers persistants `data/` et `anki-data/` ;
3. construit l'image du bot et du dashboard ;
4. télécharge l'image Anki ;
5. valide la configuration et initialise/migre SQLite ;
6. démarre les trois conteneurs.

Contrôlez leur état :

```bash
docker compose --profile anki ps
docker compose logs -f english-teacher dashboard anki
```

## 4. Initialiser Anki et AnkiConnect

Depuis votre ordinateur, ouvrez un tunnel vers le serveur :

```bash
ssh -L 3000:127.0.0.1:3000 utilisateur@serveur
```

Ouvrez ensuite `http://localhost:3000`. Au premier lancement, le lanceur Anki peut
demander de saisir `1`, puis Entrée, afin de télécharger Anki.

Dans Anki :

1. créez ou ouvrez votre profil ;
2. ouvrez **Tools → Add-ons → Get Add-ons** ;
3. installez AnkiConnect avec le code `2055492159` ;
4. redémarrez Anki depuis son interface ;
5. vérifiez qu'AnkiConnect écoute sur `0.0.0.0:8765` dans sa configuration.

Le port 8765 n'est pas publié sur Internet : il est accessible seulement sur le réseau
Docker par le bot. Vous pouvez donc laisser `ANKICONNECT_API_KEY` vide. Si vous définissez
une clé `apiKey` dans AnkiConnect, recopiez exactement la même valeur dans `.env`.

Testez AnkiConnect depuis le conteneur du bot :

```bash
docker compose exec english-teacher python -c "import os,httpx; p={'action':'version','version':6}; k=os.getenv('ANKICONNECT_API_KEY'); p.update({'key':k}) if k else None; print(httpx.post('http://anki:8765',json=p).json())"
```

La réponse doit contenir un numéro dans `result` et `error: null`.

## 5. Synchroniser avec téléphone ou ordinateur

La méthode la plus simple est AnkiWeb : cliquez sur le bouton de synchronisation dans
Anki Desktop et connectez votre compte. Lors de la toute première synchronisation,
choisissez soigneusement **Upload** si la collection du serveur doit remplacer une
collection AnkiWeb vide, ou **Download** si AnkiWeb contient déjà vos cartes.

Une fois la synchronisation initiale terminée, modifiez `.env` :

```dotenv
ANKI_ENABLED=true
ANKI_SYNC_AFTER_PUSH=true
```

Puis redémarrez le bot et le dashboard :

```bash
docker compose up -d --force-recreate english-teacher dashboard
```

Le serveur peut rester entièrement administré en ligne de commande. L'interface
graphique d'Anki n'est nécessaire dans le navigateur que pour l'installation initiale
d'AnkiConnect et la connexion à AnkiWeb. Ensuite, valider 5 à 10 cartes dans Streamlit
les crée dans l'instance Anki du conteneur et lance automatiquement `sync`. Le téléphone
les récupère lors de sa prochaine synchronisation Anki.

Si AnkiWeb refuse la synchronisation, confirmez d'abord l'adresse e-mail du compte,
ouvrez l'interface Anki une fois et terminez la synchronisation initiale. Les cartes
restent dans la collection du serveur même si cette étape distante échoue.

## 6. Accéder à Streamlit

Le dashboard n'a pas d'authentification intégrée. Ne publiez pas directement son port.
Utilisez un second tunnel SSH :

```bash
ssh -L 8501:127.0.0.1:8501 utilisateur@serveur
```

Puis ouvrez `http://localhost:8501`.

## 7. Mise à jour et sauvegarde

Mise à jour :

```bash
git pull
docker compose --profile anki pull
docker compose --profile anki up -d --build
```

Pour une sauvegarde cohérente, arrêtez brièvement les services et archivez les deux
dossiers persistants :

```bash
docker compose --profile anki stop
tar -czf english-teacher-backup.tgz data anki-data .env
docker compose --profile anki up -d
```

Le fichier de sauvegarde contient des secrets et doit être chiffré ou conservé dans un
emplacement privé.

## Mode sans Anki

Pour commencer sans Anki, gardez `ANKI_ENABLED=false` et démarrez uniquement :

```bash
docker compose up -d --build english-teacher dashboard
```

Les propositions de cartes resteront enregistrées dans SQLite et pourront être envoyées
plus tard après l'activation d'Anki.
