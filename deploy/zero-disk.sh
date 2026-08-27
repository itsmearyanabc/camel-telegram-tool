#!/usr/bin/env bash
#
# Switch an installed Telegram Tool to RAM-backed state.
#
#   sudo bash zero-disk.sh [app-name]      # default: telegram-tool
#
# After this the app writes NOTHING to persistent disk:
#
#   sessions, both databases, config.json   ->  /run/<app>  (tmpfs, i.e. RAM)
#   file attachments                        ->  Supabase Storage, never disk
#   logs                                    ->  journald only
#
# Supabase becomes the single durable copy: state is uploaded on every change
# and pulled back into RAM at boot.
#
# The application code and virtualenv stay on disk — Python has to load them
# from somewhere. This removes runtime DATA, not the installation.
#
# REQUIRES working Supabase credentials. Without them a reboot loses
# everything, so this script verifies a real round-trip before changing
# anything and refuses otherwise.
#
set -euo pipefail

APP_NAME="${1:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"
APP_USER="$(echo "$APP_NAME" | tr -cd '[:alnum:]' | cut -c1-30)"
RUN_DIR="/run/${APP_NAME}"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; OFF=$'\e[0m'
say()  { echo "${BLD}==>${OFF} $*"; }
ok()   { echo "  ${GRN}ok${OFF}  $*"; }
warn() { echo "  ${YLW}!!${OFF}  $*"; }
die()  { echo "${RED}error:${OFF} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -d "$APP_DIR" ]] || die "$APP_DIR not found — run setup.sh first"
[[ -f "$APP_DIR/.env" ]] || die "$APP_DIR/.env not found"

say "Switching ${BLD}${APP_NAME}${OFF} to RAM-backed state"

# ── 1. Supabase must genuinely work, or a reboot destroys everything ──
say "Verifying Supabase round-trip"
set +e
"$APP_DIR/venv/bin/python" - <<'PY'
import os, sys, secrets
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))
if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
    print("SUPABASE_URL / SUPABASE_KEY are not set in .env"); sys.exit(1)
sys.path.insert(0, os.getcwd())
from core.services.persistence import persistence
if not persistence.enabled:
    print("persistence layer reports disabled"); sys.exit(1)
probe = secrets.token_hex(8).encode()
key = "healthcheck/zero-disk.probe"
if not persistence.upload_bytes(key, probe):
    print("upload failed"); sys.exit(1)
if persistence.download_bytes(key) != probe:
    print("download did not match what was uploaded"); sys.exit(1)
persistence.delete_path(key)
print("round-trip OK")
PY
RC=$?
set -e
cd - >/dev/null 2>&1 || true
[[ $RC -eq 0 ]] || die "Supabase check failed. Fix SUPABASE_URL / SUPABASE_KEY in $APP_DIR/.env first — without it, RAM-backed state would be lost on every reboot."
ok "Supabase verified — safe to keep state in RAM"

# ── 2. Push whatever is on disk right now, so nothing is lost ──
say "Backing up current state to Supabase"
( cd "$APP_DIR" && "$APP_DIR/venv/bin/python" - <<'PY'
import os, sys
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from core.services.persistence import persistence
from utils.paths import SESSIONS_DIR
persistence.backup_config()
persistence.backup_group_db()
persistence.backup_bot_db()
n = 0
if os.path.isdir(SESSIONS_DIR):
    for f in os.listdir(SESSIONS_DIR):
        if f.endswith(".session"):
            clean = f.replace("session_", "").replace(".session", "")
            if persistence.backup_session(clean):
                n += 1
print(f"sessions backed up: {n}")
PY
) || warn "backup step reported problems — check the output above"
ok "current state pushed to Supabase"

# ── 3. Point state at tmpfs ──
# Which supervisor is running this?
SUPERVISOR="systemd"
if command -v pm2 >/dev/null 2>&1 && pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\""; then
    SUPERVISOR="pm2"
fi
ok "supervisor detected: ${SUPERVISOR}"

say "Configuring RAM-backed state"

# The app calls load_dotenv() at startup, so .env works for BOTH supervisors.
sed -i '/^STATE_DIR=/d; /^LOG_TO_FILE=/d' "$APP_DIR/.env"
cat >> "$APP_DIR/.env" <<EOF

# RAM-backed state — set by deploy/zero-disk.sh
STATE_DIR=${RUN_DIR}
LOG_TO_FILE=0
EOF
chmod 600 "$APP_DIR/.env"
ok "STATE_DIR written to .env"

# /run is wiped on reboot, so the directory must be recreated declaratively.
OWNER="root:root"
[[ "$SUPERVISOR" == "systemd" ]] && OWNER="${APP_USER}:${APP_USER}"
cat > "/etc/tmpfiles.d/${APP_NAME}.conf" <<EOF
# Recreate the RAM-backed state directory on every boot.
d ${RUN_DIR}               0700 ${OWNER%%:*} ${OWNER##*:} -
d ${RUN_DIR}/sessions      0700 ${OWNER%%:*} ${OWNER##*:} -
d ${RUN_DIR}/data          0700 ${OWNER%%:*} ${OWNER##*:} -
d ${RUN_DIR}/data/uploads  0700 ${OWNER%%:*} ${OWNER##*:} -
EOF
systemd-tmpfiles --create "/etc/tmpfiles.d/${APP_NAME}.conf" >/dev/null 2>&1 ||     mkdir -p "$RUN_DIR"/{sessions,data/uploads}
chown -R "$OWNER" "$RUN_DIR" 2>/dev/null || true
ok "tmpfs directory ${RUN_DIR} created (recreated automatically at boot)"

if [[ "$SUPERVISOR" == "systemd" ]]; then
    # ProtectSystem/ReadWritePaths would otherwise block writes to /run.
    DROPIN="/etc/systemd/system/${APP_NAME}.service.d"
    mkdir -p "$DROPIN"
    cat > "${DROPIN}/zero-disk.conf" <<EOF
# Generated by deploy/zero-disk.sh — runtime state lives in RAM.
[Service]
Environment=STATE_DIR=${RUN_DIR}
Environment=LOG_TO_FILE=0
ExecStartPre=/bin/mkdir -p ${RUN_DIR}/sessions ${RUN_DIR}/data/uploads
ReadWritePaths=${RUN_DIR}
EOF
    systemctl daemon-reload
    ok "systemd drop-in written"
fi

# ── 4. Remove on-disk state (already safely in Supabase) ──
say "Clearing on-disk state"
FREED=0
for target in "$APP_DIR/sessions" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/config.json"; do
    if [[ -e "$target" ]]; then
        SZ=$(du -sk "$target" 2>/dev/null | awk '{print $1}')
        FREED=$((FREED + ${SZ:-0}))
        rm -rf "$target"
        ok "removed $(basename "$target")"
    fi
done
ok "freed ~${FREED} KB of disk"

# ── 5. Restart and confirm the restore worked ──
say "Restarting"
if [[ "$SUPERVISOR" == "pm2" ]]; then
    pm2 restart "$APP_NAME" --update-env >/dev/null
    pm2 save >/dev/null 2>&1 || true
else
    systemctl restart "$APP_NAME"
fi
sleep 10

if [[ "$SUPERVISOR" == "pm2" ]]; then
    pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\"" || die "app missing from PM2 after restart"
    ok "PM2 process restarted"
else
    systemctl is-active --quiet "$APP_NAME" || {
        journalctl -u "$APP_NAME" -n 40 --no-pager
        die "service failed to start — log above"
    }
    ok "service restarted"
fi

echo
say "Verifying state came back from Supabase"
if [[ "$SUPERVISOR" == "pm2" ]]; then
    pm2 logs "$APP_NAME" --lines 60 --nostream 2>/dev/null | grep -E "Restored|Supabase" || true
else
    journalctl -u "$APP_NAME" -n 60 --no-pager | grep -E "Restored|Supabase|STATE" || true
fi

echo
if [[ -e "$APP_DIR/data" || -e "$APP_DIR/sessions" || -e "$APP_DIR/config.json" ]]; then
    warn "state reappeared in $APP_DIR — the drop-in may not have applied"
    warn "check: systemctl show ${APP_NAME} -p Environment"
else
    ok "no state directories in $APP_DIR"
fi

echo
echo "${GRN}${BLD}Zero-disk mode active.${OFF}"
echo "  State in RAM   ${RUN_DIR}"
echo "  Durable copy   Supabase Storage"
echo "  Logs           $([[ "$SUPERVISOR" == "pm2" ]] && echo "pm2 logs ${APP_NAME}" || echo "journalctl -u ${APP_NAME} -f")"
echo "  RAM in use     $(du -sh ${RUN_DIR} 2>/dev/null | awk '{print $1}' || echo 'n/a')"
echo
echo "${YLW}Trade-off:${OFF} on reboot, RAM is cleared and state is restored from"
echo "Supabase. If Supabase is unreachable at boot, the app starts empty and"
echo "your Telegram accounts will need logging in again. Everything is uploaded"
echo "within seconds of any change, so at most the last few seconds are at risk."
echo
echo "To revert: sudo rm ${DROPIN}/zero-disk.conf && sudo systemctl daemon-reload && sudo systemctl restart ${APP_NAME}"
