#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${HOME}/qlab-venv"
VENV_PY="${VENV_DIR}/bin/python"

usage() {
  cat <<'USAGE'
Usage: ./update [--branch <branch_name>]

Par défaut, la branche cible est "main".
Exemple machine de test:
  ./update --branch dev-beta
USAGE
}

echo "📍 REPO_DIR=${REPO_DIR}"
cd "$REPO_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ Ce dossier n'est pas un dépôt Git valide: $REPO_DIR" >&2
  exit 1
fi

TARGET_BRANCH="main"

while [ "$#" -gt 0 ]; do
  case "$1" in
    -b|--branch)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "❌ Valeur de branche manquante pour $1" >&2
        usage
        exit 1
      fi
      TARGET_BRANCH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "❌ Argument inconnu: $1" >&2
      usage
      exit 1
      ;;
  esac
done

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "🌿 Branche courante: ${CURRENT_BRANCH}"
echo "🎯 Branche cible de déploiement: ${TARGET_BRANCH}"

if [ -n "$(git status --porcelain)" ]; then
  echo "❌ Des modifications locales non commitées sont présentes." >&2
  echo "   Commit, stash, ou annule les changements avant d'exécuter update." >&2
  exit 1
fi

echo "🔄 Synchronisation depuis origin/${TARGET_BRANCH}..."
git fetch --verbose origin "$TARGET_BRANCH"

if [ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]; then
  echo "↪️ Bascule vers ${TARGET_BRANCH}"
  if ! git checkout "$TARGET_BRANCH"; then
    git checkout -b "$TARGET_BRANCH" --track "origin/$TARGET_BRANCH"
  fi
fi

if ! git pull --ff-only --verbose origin "$TARGET_BRANCH"; then
  echo "⚠️ Historique divergent détecté (ex: forced update sur origin/${TARGET_BRANCH})."
  echo "🔁 Resynchronisation locale stricte sur origin/${TARGET_BRANCH}..."
  git reset --hard "origin/$TARGET_BRANCH"
fi

echo "🐍 Vérification du venv..."
if [ ! -x "$VENV_PY" ]; then
  echo "⚠️ Venv introuvable (${VENV_PY}). Lance d'abord ./deploy/install.sh" >&2
  exit 2
fi

REQ_FILE="${REPO_DIR}/deploy/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
  REQ_FILE="${REPO_DIR}/requirements.txt"
fi

if [ ! -f "$REQ_FILE" ]; then
  echo "❌ Fichier de dépendances introuvable (attendu: deploy/requirements.txt ou requirements.txt)." >&2
  exit 3
fi

echo "📦 Installation des dépendances depuis ${REQ_FILE}"
"${VENV_DIR}/bin/pip" install -r "$REQ_FILE"

echo "🧩 Déploiement (service systemd)..."
SERVICE_SRC="${REPO_DIR}/deploy/systemd/qlab-box.service"
SERVICE_TMP="$(mktemp)"
sed -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__VENV_PY__|${VENV_PY}|g" \
    "${SERVICE_SRC}" > "${SERVICE_TMP}"
sudo cp -f "${SERVICE_TMP}" /etc/systemd/system/qlab-box.service
rm -f "${SERVICE_TMP}"
sudo systemctl daemon-reload
sudo systemctl restart qlab-box

set +e
sudo systemctl --no-pager --full status qlab-box
STATUS_RC=$?
set -e

if [ $STATUS_RC -ne 0 ]; then
  echo "❌ Le service qlab-box est en échec."
  echo "   Logs utiles: journalctl -u qlab-box -n 100 --no-pager"
  exit $STATUS_RC
fi

echo "✅ Mise à jour terminée et service actif."
