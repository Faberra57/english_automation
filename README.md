# English Teacher Bot

Un professeur d'anglais privé piloté depuis Telegram. Il archive chaque texte et chaque audio, transcrit les voix avec Groq/Whisper, corrige avec DeepSeek, récupère les faiblesses antérieures depuis SQLite et crée des fiches Anki le soir.

## Organisation du code

```text
bot.py                         # point d'entrée de compatibilité
english_teacher/
├── __main__.py                # exécution avec python -m english_teacher
├── main.py                    # démarrage et validation de configuration
├── config.py                  # lecture/validation de .env
├── database.py                # schéma et accès SQLite/RAG
├── clients.py                 # DeepSeek, Groq et AnkiConnect
├── service.py                 # logique pédagogique et fiches
├── telegram_app.py            # commandes, messages et planification
└── utils.py                   # fonctions génériques
```

Les dépendances suivent un seul sens : Telegram appelle le service, le service orchestre les clients et la base, et les couches basses ne dépendent jamais de Telegram.

## Architecture

```text
Telegram ──texte/audio──> bot Python
                            ├── archive brute + SQLite (RAG)
                            ├── Groq Whisper (audio → texte)
                            ├── DeepSeek (correction/sujet/fiches)
                            └── AnkiConnect → Anki Desktop → serveur Anki Sync → iPhone
```

SQLite est la source de vérité. Le RAG sélectionne les erreurs avec un score combinant pertinence lexicale, répétition, sévérité et récence. Cela garde le déploiement léger et totalement local pour les données; seuls Telegram, Groq et DeepSeek reçoivent les éléments nécessaires à leur étape.

## Point important sur Anki

AnkiConnect et un serveur Anki Sync sont deux composants différents :

- **AnkiConnect** est une extension REST chargée dans Anki Desktop. Elle crée les notes et propose l'action `sync`.
- **Anki Sync Server** synchronise une collection existante entre clients; il ne fournit pas l'API AnkiConnect.

Il faut donc une instance Anki Desktop persistante sur le serveur (éventuellement dans un conteneur avec interface web/Xvfb), avec AnkiConnect installé et le profil configuré pour votre serveur Sync. Le bot contacte cette instance sur le port 8765. Écrire directement dans `collection.anki2` est volontairement évité : cela risquerait les conflits et la corruption.

Si AnkiConnect est arrêté au moment de la tâche du soir, les cartes restent dans SQLite avec l'état `pending`/`failed`. La tâche de reprise et la commande `/cards` les renvoient plus tard sans doublons.

## Installation

Prérequis : Docker Engine avec le plugin Compose, un bot créé auprès de `@BotFather`, les clés DeepSeek/Groq et une instance Anki Desktop + AnkiConnect si l'export Anki est activé. Pour le développement local, utilisez `uv` (aucun `pip install` n'est nécessaire).

```bash
cp .env.example .env
mkdir -p data/audio
chmod 700 data
```

Éditez `.env`, au minimum :

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `TELEGRAM_CHAT_ID`
- `DEEPSEEK_API_KEY`
- `GROQ_API_KEY`

Le chat privé avec le bot a généralement le même ID numérique que l'utilisateur. En cas de doute, démarrez temporairement le bot avec un ID fictif et envoyez `/start` : la réponse d'accès refusé affiche l'ID à autoriser.

Validez puis démarrez :

```bash
docker compose build
docker compose run --rm english-teacher python bot.py --check
docker compose up -d
docker compose logs -f english-teacher
```

Pour lancer le bot ou les tests hors Docker :

```bash
uv sync --locked
uv run --locked python -m english_teacher --check
uv run --locked python -m pytest
```

Le `network_mode: host` est adapté au serveur Linux demandé et permet au conteneur d'atteindre AnkiConnect sur `127.0.0.1:8765`. Si Anki Desktop est lui-même dans un autre conteneur, retirez `network_mode: host`, placez les deux services sur le même réseau Compose et mettez par exemple `ANKICONNECT_URL=http://anki-desktop:8765`.

### Préparer AnkiConnect

1. Installez AnkiConnect dans l'instance Anki Desktop qui restera active.
2. Créez ou ouvrez le profil synchronisé avec votre serveur Anki Sync.
3. Gardez AnkiConnect lié à `127.0.0.1` si vous utilisez le réseau hôte.
4. Si vous configurez une clé dans AnkiConnect, recopiez-la dans `ANKICONNECT_API_KEY`.
5. Vérifiez que le type de note et les champs de `.env` existent (`Basic`, `Front`, `Back` par défaut).

Pour désactiver temporairement l'intégration : `ANKI_ENABLED=false`. Les fiches sont tout de même générées et conservées en attente.

## Commandes Telegram

- Envoyer un texte : archivage et correction immédiate.
- Envoyer une note vocale, un audio ou un document audio : archivage du fichier original, transcription archivée, puis correction.
- `/topic` : force la génération du sujet du jour.
- `/cards` : crée les fiches du jour puis retente toute la file Anki.
- `/stats` : compteurs de la mémoire locale.
- `/retry 42` : retente la transcription/correction de la production 42.

Le bot ignore tous les comptes qui ne figurent pas dans `TELEGRAM_ALLOWED_USER_IDS`.

## Données et sauvegarde

Tout est sous `./data` :

```text
data/
├── english_teacher.sqlite3
├── english_teacher.sqlite3-wal
├── english_teacher.sqlite3-shm
└── audio/YYYY/MM/DD/...
```

La base contient les textes originaux, légendes, transcriptions détaillées, corrections JSON, erreurs, sujets, fiches et états Anki. Les fichiers audio ont un SHA-256 et leur chemin relatif est enregistré. `AUDIO_RETENTION_DAYS=0` signifie conservation illimitée; aucune purge automatique n'est actuellement exécutée, même si une autre valeur est définie.

Pour une sauvegarde simple et cohérente, arrêtez brièvement le service, sauvegardez tout `data/`, puis redémarrez-le :

```bash
docker compose stop english-teacher
tar -czf english-teacher-backup.tgz data
docker compose start english-teacher
```

## Réglages

`.env.example` documente tous les paramètres : horaires et jours, fuseau, modèles, timeouts/reprises, profil pédagogique, taille du contexte RAG, limite d'erreurs, limite de cartes, deck/champs/tags Anki et API Telegram locale.

Après modification de `.env` :

```bash
docker compose up -d --force-recreate english-teacher
```

## Telegram ou une alternative auto-hébergée ?

Telegram reste le meilleur compromis ici pour les notifications push, les notes vocales et la simplicité sur iPhone. Le bot et les données sont auto-hébergés, mais le transport Telegram ne l'est pas : les messages passent par Telegram avant d'être archivés localement.

Pour un transport réellement auto-hébergé, **Matrix + Synapse + Element** est l'alternative la plus cohérente. Elle demande toutefois un bot Matrix distinct, un serveur accessible en HTTPS, la gestion du chiffrement et plus de maintenance; l'expérience des notes vocales est moins directe. Nextcloud Talk est envisageable si vous l'exploitez déjà. Signal n'offre pas une API bot officielle comparable. Le moteur de ce projet peut être conservé et seule la couche des gestionnaires Telegram devra être remplacée.

## Limites connues

- Whisper fournit une transcription, pas une mesure acoustique fiable de la prononciation. Le bot ne prétend donc pas noter accent, rythme ou intonation.
- Avec l'API Bot Telegram hébergée, le téléchargement d'un fichier par un bot est limité; la valeur par défaut du projet reste sous cette limite.
- La mémoire RAG actuelle est locale et légère, mais lexicale. Pour de la recherche sémantique multilingue à grande échelle, on pourra ajouter ultérieurement un modèle d'embeddings local et Chroma/Qdrant sans changer le schéma métier SQLite.
