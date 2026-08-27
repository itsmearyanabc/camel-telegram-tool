#!/usr/bin/env bash
#
# Pull the latest code and restart. Run from anywhere:
#   sudo bash /opt/telegram-tool/deploy/update.sh [app-name]
#
# Never touches .env, sessions/ or data/ — your logins, user pool and
# monitored groups survive every update.
#
set -euo pipefail

APP_NAME="${1:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"
APP_USER="$(echo "$APP_NAME" | tr -cd '[:alnum:]' | cut -c1-30)"

GRN=$'\e[32m'; BLD=$'\e[1m'; OFF=$'\e[0m'

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "${BLD}==>${OFF} Fetching latest code"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
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
    APP_PORT="$(grep -oP 'bind 127\.0\.0\.1:\K[0-9]+' "/etc/systemd/system/${APP_NAME}.service" 2>/dev/null || echo 5001)"
    sed -e "s#/opt/armedias#${APP_DIR}#g"         -e "s/^User=armedias/User=${APP_USER}/"         -e "s/^Group=armedias/Group=${APP_USER}/"         -e "s/127\.0\.0\.1:5001/127.0.0.1:${APP_PORT}/"         -e "s/^SyslogIdentifier=armedias/SyslogIdentifier=${APP_NAME}/"         "$APP_DIR/deploy/armedias.service" > "/etc/systemd/system/${APP_NAME}.service"
    systemctl daemon-reload
fi

mkdir -p "$APP_DIR"/{sessions,logs,data/uploads}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "${BLD}==>${OFF} Restarting"
systemctl restart "$APP_NAME"
sleep 5

if systemctl is-active --quiet "$APP_NAME"; then
    echo "${GRN}${BLD}Updated and running.${OFF}  Logs: journalctl -u $APP_NAME -f"
else
    echo "Service failed to start:" >&2
    journalctl -u "$APP_NAME" -n 40 --no-pager >&2
    exit 1
fi
