#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(eval echo "~${USER_NAME}")"

echo "==> apt deps"
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip python3-lgpio

echo "==> create venv (system site packages)"
if [ ! -d "${HOME_DIR}/qlab-venv" ]; then
  python3 -m venv --system-site-packages "${HOME_DIR}/qlab-venv"
fi

echo "==> pip deps"
"${HOME_DIR}/qlab-venv/bin/pip" install --upgrade pip
"${HOME_DIR}/qlab-venv/bin/pip" install -r "${REPO_DIR}/deploy/requirements.txt"

echo "==> systemd service"
SERVICE_SRC="${REPO_DIR}/deploy/systemd/qlab-box.service"
SERVICE_TMP="$(mktemp)"
sed -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__VENV_PY__|${HOME_DIR}/qlab-venv/bin/python|g" \
    "${SERVICE_SRC}" > "${SERVICE_TMP}"
sudo cp -f "${SERVICE_TMP}" /etc/systemd/system/qlab-box.service
rm -f "${SERVICE_TMP}"
sudo systemctl daemon-reload
sudo systemctl enable --now qlab-box

echo "==> sudoers (restart only)"
sudo install -m 0440 /dev/null /etc/sudoers.d/qlab-box
echo "${USER_NAME} ALL=NOPASSWD: /bin/systemctl restart qlab-box" | sudo tee /etc/sudoers.d/qlab-box >/dev/null

echo "==> done"
systemctl status qlab-box --no-pager || true
