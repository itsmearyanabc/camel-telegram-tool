#!/usr/bin/env bash
#
# Pull the latest code and restart. Run from anywhere:
#   sudo bash /opt/armedias/deploy/update.sh
#
# Never touches .env, sessions/ or data/ — your logins, user pool and
# monitored groups survive every update.
#
set -euo pipefail

APP_DIR="/opt/armedias"
APP_USER="armedias"

GRN=$'\e[32m'; BLD=$'\e[1m'; OFF=$'\e[0m'

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "${BLD}==>${OFF} Fetching latest code"
BEFORE="$(git -C "$APP_DIR" rev-parse --short HEAD)"
git -C "$APP_DIR" fetch --all -q
git -C "$APP_DIR" reset --hard origin/main -q
AFTER="$(git -C "$APP_DIR" rev-parse --short HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "  already up to date ($AFTER)"
else
    echo "  $BEFORE -> $AFTER"
fi

echo "${BLD}==>${OFF} Syncing dependencies"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# A new release may add deploy config; keep the installed copies current.
if [[ -f "$APP_DIR/deploy/armedias.service" ]]; then
    install -m 644 "$APP_DIR/deploy/armedias.service" /etc/systemd/system/armedias.service
    systemctl daemon-reload
fi

mkdir -p "$APP_DIR"/{sessions,logs,data/uploads}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "${BLD}==>${OFF} Restarting"
systemctl restart armedias
sleep 5

if systemctl is-active --quiet armedias; then
    echo "${GRN}${BLD}Updated and running.${OFF}  Logs: journalctl -u armedias -f"
else
    echo "Service failed to start:" >&2
    journalctl -u armedias -n 40 --no-pager >&2
    exit 1
fi
