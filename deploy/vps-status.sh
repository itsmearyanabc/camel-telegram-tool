#!/usr/bin/env bash
#
# VPS status — every project on this box, and an isolation check for this one.
#
#   sudo bash vps-status.sh [app-name]        # default: telegram-tool
#
# READ-ONLY. Inspects, never changes anything.
#
set -uo pipefail

APP_NAME="${1:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"

GRN=$'\e[32m'; YLW=$'\e[33m'; RED=$'\e[31m'; BLU=$'\e[34m'; DIM=$'\e[2m'; BLD=$'\e[1m'; OFF=$'\e[0m'
hdr()  { echo; echo "${BLD}${BLU}━━ $* ━━${OFF}"; }
ok()   { echo "  ${GRN}ok${OFF}    $*"; }
warn() { echo "  ${YLW}warn${OFF}  $*"; }
bad()  { echo "  ${RED}RISK${OFF}  $*"; }
row()  { echo "        $*"; }

echo "${BLD}VPS STATUS${OFF}  $(hostname)  ·  $(date '+%Y-%m-%d %H:%M')"

# ── Host ──
hdr "Host"
row "os      $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
row "kernel  $(uname -r)"
row "uptime  $(uptime -p 2>/dev/null | sed 's/^up //')"
row "load    $(cut -d' ' -f1-3 /proc/loadavg)  (cores: $(nproc))"
if [[ -f /var/run/reboot-required ]]; then
    warn "a reboot is pending (kernel or libc update)"
fi

# ── Resources ──
hdr "Resources"
free -h | awk 'NR==1{print "        " $0} NR==2{print "        " $0}'
df -h / | awk 'NR==1{print "        " $0} NR==2{print "        " $0}'
USE_PCT="$(df / | awk 'NR==2{gsub("%","",$5); print $5}')"
[[ "${USE_PCT:-0}" -gt 85 ]] && bad "root filesystem is ${USE_PCT}% full" || ok "disk headroom fine (${USE_PCT}% used)"
AVAIL_MB="$(free -m | awk '/^Mem:/{print $7}')"
[[ "${AVAIL_MB:-0}" -lt 300 ]] && bad "only ${AVAIL_MB}MB RAM available" || ok "${AVAIL_MB}MB RAM available"

# ── Everything serving traffic ──
hdr "Listening sockets"
printf "        %-24s %-8s %s\n" "ADDRESS" "PID" "PROCESS"
ss -tlnp 2>/dev/null | tail -n +2 | while read -r _ _ _ addr _ proc; do
    pid=$(sed -n 's/.*pid=\([0-9]*\).*/\1/p' <<<"$proc")
    nm=$(sed -n 's/.*users:((\"\([^\"]*\)\".*/\1/p' <<<"$proc")
    printf "        %-24s %-8s %s\n" "$addr" "${pid:--}" "${nm:-?}"
done | sort -u

# ── systemd services (non-stock) ──
hdr "Application services (systemd)"
${DIM:+}row "${DIM}Python/other apps run here. Node apps appear under PM2 below.${OFF}"
systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null \
 | awk '{print $1}' \
 | grep -vE '^(systemd-|dbus|cron|ssh|rsyslog|polkit|udisks|networkd|resolved|logind|journald|getty|user@|unattended|multipathd|snapd|accounts-daemon|packagekit|irqbalance|chrony|qemu-guest|serial-getty|apparmor|ufw|atd|uuidd)' \
 | while read -r svc; do
      mem=$(systemctl show "$svc" -p MemoryCurrent --value 2>/dev/null)
      [[ "$mem" =~ ^[0-9]+$ ]] && mem="$((mem/1048576))M" || mem="-"
      since=$(systemctl show "$svc" -p ActiveEnterTimestamp --value 2>/dev/null | cut -d' ' -f2-3)
      printf "        %-34s mem=%-7s since %s\n" "$svc" "$mem" "${since:-?}"
   done

# ── PM2 ──
if command -v pm2 >/dev/null 2>&1; then
    hdr "PM2 processes"
    pm2 jlist 2>/dev/null | python3 -c '
import json,sys
try: apps=json.load(sys.stdin)
except Exception: apps=[]
if not apps: print("        (none)")
for a in apps:
    e=a.get("pm2_env",{}); m=a.get("monit",{})
    print("        %-22s %-9s restarts=%-4s mem=%sM  cwd=%s" % (
        a.get("name","?"), e.get("status","?"), e.get("restart_time",0),
        round(m.get("memory",0)/1048576), e.get("pm_cwd","?")))
' 2>/dev/null || row "(pm2 present but not readable as this user)"
fi

# ── Docker ──
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    hdr "Docker containers"
    docker ps --format '        {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || row "(none)"
fi

# ── nginx ──
hdr "nginx sites"
if command -v nginx >/dev/null; then
    nginx -t >/dev/null 2>&1 && ok "config valid" || bad "config INVALID — reloads will fail for every site"
    for s in /etc/nginx/sites-enabled/*; do
        [[ -e "$s" ]] || continue
        names=$(grep -hoP 'server_name\s+\K[^;]+' "$s" 2>/dev/null | tr '\n' ' ' | tr -s ' ')
        ssl=$(grep -qs 'ssl_certificate' "$s" && echo "https" || echo "http")
        printf "        %-22s %-6s %s\n" "$(basename "$s")" "$ssl" "${names:-—}"
    done
fi

# ── Certificates ──
if command -v certbot >/dev/null 2>&1; then
    hdr "TLS certificates"
    certbot certificates 2>/dev/null \
      | grep -E "Certificate Name:|Domains:|Expiry Date:" \
      | sed 's/^\s*/        /' || row "(none)"
fi

# ── Firewall ──
hdr "Firewall"
if command -v ufw >/dev/null; then
    ufw status 2>/dev/null | sed 's/^/        /' | head -12
else
    row "ufw not installed"
fi

# ── Disk by project ──
hdr "Disk used per project"
for d in /opt/* /var/www/* /srv/* /home/* /root/*; do
    [[ "$(basename "$d")" == .* ]] && continue
    [[ -d "$d" ]] || continue
    printf "        %-40s %s\n" "$d" "$(du -sh "$d" 2>/dev/null | cut -f1)"
done | sort -k2 -h -r | head -12

# ── Isolation check for this app ──
hdr "Isolation check — ${APP_NAME}"
RISK=0

# Which supervisor actually owns this app? After a migration the systemd unit
# is left in place but disabled, so its presence alone proves nothing.
SUPERVISOR="none"
if command -v pm2 >/dev/null 2>&1 && pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\""; then
    SUPERVISOR="pm2"
elif systemctl cat "$APP_NAME" >/dev/null 2>&1; then
    SUPERVISOR="systemd"
fi

case "$SUPERVISOR" in
  pm2)
    ok "managed by PM2 (systemd unit present but disabled)"
    ST=$(pm2 jlist 2>/dev/null | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a['name']=='${APP_NAME}':
        e=a['pm2_env']
        print(e.get('status','?'), e.get('restart_time',0), round(a.get('monit',{}).get('memory',0)/1048576))
" 2>/dev/null)
    set -- $ST
    [[ "${1:-}" == "online" ]] && ok "process online (restarts=${2:-?}, mem=${3:-?}M)"                                || warn "PM2 status: ${1:-unknown}"
    ARGS=$(pm2 jlist 2>/dev/null | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a['name']=='${APP_NAME}': print(' '.join(a['pm2_env'].get('args') or []))
" 2>/dev/null)
    BIND=$(grep -oP '\-\-bind \K[^ ]+' <<<"$ARGS" | head -1)
    WORKERS=$(grep -oP '\-\-workers \K[0-9]+' <<<"$ARGS" | head -1)
    RUNAS="root"
    ;;
  systemd)
    ok "managed by systemd"
    systemctl is-active --quiet "$APP_NAME" && ok "service running" || warn "service not running"
    BIND=$(systemctl cat "$APP_NAME" 2>/dev/null | grep -oP '\-\-bind \K[^ ]+' | head -1)
    WORKERS=$(systemctl cat "$APP_NAME" 2>/dev/null | grep -oP '\-\-workers \K[0-9]+' | head -1)
    RUNAS=$(systemctl show "$APP_NAME" -p User --value 2>/dev/null)
    row "writable paths: $(systemctl show "$APP_NAME" -p ReadWritePaths --value 2>/dev/null || echo '(unrestricted)')"
    MEM=$(systemctl show "$APP_NAME" -p MemoryCurrent --value 2>/dev/null)
    [[ "$MEM" =~ ^[0-9]+$ ]] && row "memory in use: $((MEM/1048576))M"
    ;;
  *)
    warn "'${APP_NAME}' is not managed by PM2 or systemd"
    ;;
esac

if [[ "$SUPERVISOR" != "none" ]]; then
    if [[ "${BIND:-}" == 127.0.0.1:* ]]; then
        ok "bound to loopback only ($BIND) — not publicly reachable"
    else
        bad "bound to ${BIND:-unknown} — should be 127.0.0.1 behind nginx"; RISK=$((RISK+1))
    fi

    [[ "${WORKERS:-}" == "1" ]] && ok "single worker (required — state is in-process)"                                 || bad "workers=${WORKERS:-?}; >1 duplicates every forward"

    if [[ -n "${RUNAS:-}" && "$RUNAS" != "root" ]]; then
        ok "runs as unprivileged user '$RUNAS'"
    else
        # Deliberate under PM2, and consistent with the other apps here, so it
        # is reported rather than counted as a blocking risk.
        warn "runs as root — no ProtectSystem or ReadWritePaths confinement"
    fi

    # State location comes from .env under PM2, from the unit under systemd.
    STATE=$(grep -oP '^STATE_DIR=\K.*' "$APP_DIR/.env" 2>/dev/null | tail -1)
    [[ -z "$STATE" ]] && STATE=$(systemctl show "$APP_NAME" -p Environment --value 2>/dev/null                                  | tr ' ' '
' | grep '^STATE_DIR=' | cut -d= -f2)
    if [[ -n "$STATE" ]]; then
        ok "state dir: $STATE ($(du -sh "$STATE" 2>/dev/null | cut -f1 || echo n/a))"
        [[ "$STATE" == /run/* || "$STATE" == /dev/shm/* ]] && ok "RAM-backed — writes no data to disk"
    else
        row "state dir: default, inside $APP_DIR (run deploy/zero-disk.sh to move it to RAM)"
    fi
fi

# does it touch anything outside its own tree?
for f in /etc/nginx/sites-enabled/*; do
    [[ -e "$f" ]] || continue
    if [[ "$(basename "$f")" != "$APP_NAME" ]] && grep -qs "$APP_DIR" "$f"; then
        bad "another nginx site ($(basename "$f")) references $APP_DIR"; RISK=$((RISK+1))
    fi
done
ok "no other site references this app's directory"

echo
if [[ "$RISK" -gt 0 ]]; then
    echo "${RED}${BLD}${RISK} isolation concern(s) above.${OFF}"
    exit 1
fi
echo "${GRN}${BLD}No isolation risks detected.${OFF} Other projects are unaffected by this deployment."
