#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker n'est pas installé. Installe Docker Engine et le plugin Compose." >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "Le plugin 'docker compose' n'est pas disponible." >&2
    exit 1
fi

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "Le fichier .env vient d'être créé. Remplis les clés et relance ce script." >&2
    exit 2
fi

missing=$(awk -F= '
    /^(TELEGRAM_BOT_TOKEN|TELEGRAM_ALLOWED_USER_IDS|TELEGRAM_CHAT_ID|DEEPSEEK_API_KEY|XAI_API_KEY|ELEVENLABS_API_KEY)=/ {
        seen[$1]=1
        if ($2 == "" || index($2, "replace_me") > 0) bad[$1]=1
        if (($1 == "TELEGRAM_ALLOWED_USER_IDS" || $1 == "TELEGRAM_CHAT_ID") && $2 == "123456789") bad[$1]=1
    }
    END {
        required[1]="TELEGRAM_BOT_TOKEN"; required[2]="TELEGRAM_ALLOWED_USER_IDS";
        required[3]="TELEGRAM_CHAT_ID"; required[4]="DEEPSEEK_API_KEY";
        required[5]="XAI_API_KEY"; required[6]="ELEVENLABS_API_KEY";
        for (i=1; i<=6; i++) if (!seen[required[i]] || bad[required[i]]) print required[i]
    }
' .env)
if [ -n "$missing" ]; then
    echo "Variables obligatoires absentes ou non configurées dans .env:" >&2
    echo "$missing" >&2
    exit 2
fi

mkdir -p data/audio anki-data
chmod 700 data anki-data

docker compose config --quiet
docker compose build
docker compose --profile anki pull anki
docker compose run --rm english-teacher python -m english_teacher --check
docker compose --profile anki up -d

echo "Déploiement terminé."
echo "Dashboard: ssh -L 8501:127.0.0.1:8501 <utilisateur>@<serveur>"
echo "Anki:      ssh -L 3000:127.0.0.1:3000 <utilisateur>@<serveur>"
echo "État:      docker compose --profile anki ps"
