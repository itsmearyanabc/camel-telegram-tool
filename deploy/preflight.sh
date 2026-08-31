#!/usr/bin/env bash
#
# ARMEDIAS — pre-flight inspection for a SHARED VPS
#
#   sudo bash preflight.sh telegrambot.yourdomain.com [app-name]
#
# app-name must match what you pass to setup.sh (default "telegram-tool"),
# or this checks a directory and unit the installer will never use.
#
# READ-ONLY. Changes nothing, starts nothing, installs nothing.
# Run this first on a box that already hosts other production services and
# read the output before running setup.sh.
#
set -uo pipefail

DOMAIN="${1:-}"
APP_NAME="${2:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"
APP_USER="$(echo "$APP_NAME" | tr -cd '[:alnum:]' | cut -c1-30)"
WANT_PORT=5001

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLU=$'\e[34m'; BLD=$'\e[1m'; OFF=$'\e[0m'
hdr()  { echo; echo "${BLD}${BLU}== $* ==${OFF}"; }
ok()   { echo "  ${GRN}ok${OFF}    $*"; }
warn() { echo "  ${YLW}warn${OFF}  $*"; }
bad()  { echo "  ${RED}STOP${OFF}  $*"; }
info() { echo "        $*"; }

echo "${BLD}ARMEDIAS pre-flight — read-only inspection${OFF}"
[[ -n "$DOMAIN" ]] && echo "Target subdomain: ${BLD}${DOMAIN}${OFF}"

CONFLICTS=0

# ── Identity ──
hdr "This machine"
info "hostname : $(hostname)"
info "os       : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
PUBIP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo unknown)"
info "public ip: $PUBIP"

# ── DNS ──
if [[ -n "$DOMAIN" ]]; then
    hdr "DNS"
    DIP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
    if [[ -z "$DIP" ]]; then
        warn "$DOMAIN does not resolve yet — SSL will not be issuable"
    elif [[ "$PUBIP" != "unknown" && "$DIP" != "$PUBIP" ]]; then
        bad "$DOMAIN -> $DIP but this server is $PUBIP"
        info "The A record points somewhere else. Fix it before requesting a certificate,"
        info "or Let's Encrypt will fail validation."
        CONFLICTS=$((CONFLICTS+1))
    else
        ok "$DOMAIN -> $DIP (matches this server)"
    fi
fi

# ── What is already listening ──
hdr "Ports in use"
if command -v ss >/dev/null; then
    ss -tlnp 2>/dev/null | awk 'NR>1{print "        " $4 "  " $6}' | sed 's/users:(//; s/)$//' | sort -u | head -30
else
    netstat -tlnp 2>/dev/null | tail -n +3 | awk '{print "        " $4 "  " $7}' | sort -u | head -30
fi

FREE_PORT=""
for p in $(seq 5001 5040); do
    if ! ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$p\$"; then
        FREE_PORT="$p"; break
    fi
done
echo
if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${WANT_PORT}\$"; then
    warn "port $WANT_PORT is ALREADY IN USE by another service"
    info "setup.sh will use port ${FREE_PORT:-none free} instead and leave the existing one alone"
else
    ok "port $WANT_PORT is free"
fi

# ── nginx ──
hdr "nginx"
if command -v nginx >/dev/null; then
    ok "installed: $(nginx -v 2>&1 | sed 's/nginx version: //')"
    if nginx -t >/dev/null 2>&1; then
        ok "current config is valid (a reload will be safe)"
    else
        bad "current nginx config is ALREADY INVALID — fix that before adding a site"
        nginx -t 2>&1 | sed 's/^/        /'
        CONFLICTS=$((CONFLICTS+1))
    fi
    echo
    info "existing enabled sites (these will NOT be touched):"
    for s in /etc/nginx/sites-enabled/*; do
        [[ -e "$s" ]] || continue
        SN="$(grep -hoP 'server_name\s+\K[^;]+' "$s" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')"
        info "  - $(basename "$s")  ${SN:+[$SN]}"
    done
    if [[ -n "$DOMAIN" ]] && grep -rqs "server_name.*\b${DOMAIN}\b" /etc/nginx/sites-enabled/ 2>/dev/null; then
        bad "$DOMAIN is ALREADY served by an existing nginx site"
        info "Adding another block for the same name would collide. Resolve this first."
        CONFLICTS=$((CONFLICTS+1))
    else
        ok "no existing site claims ${DOMAIN:-this subdomain}"
    fi
else
    warn "nginx not installed — setup.sh will install it"
fi

# ── firewall: the big one ──
hdr "Firewall"
if command -v ufw >/dev/null; then
    UFW_STATE="$(ufw status 2>/dev/null | head -1)"
    info "$UFW_STATE"
    if echo "$UFW_STATE" | grep -qi inactive; then
        warn "ufw is INACTIVE"
        info "setup.sh will NOT enable it. Enabling a firewall on a box with running"
        info "production services would block every port not explicitly allowed and"
        info "could take those services offline. Left for you to decide deliberately."
    else
        ok "ufw is active — setup.sh will only ADD allow rules, never enable/reset"
        ufw status 2>/dev/null | tail -n +4 | sed 's/^/        /' | head -15
    fi
else
    info "ufw not installed; nothing will be changed"
fi

# ── Collisions with our own names ──
hdr "Name collisions"
if [[ -e "$APP_DIR" ]]; then
    warn "$APP_DIR already exists"
    [[ -d "$APP_DIR/.git" ]] && info "it is a git checkout: $(git -C "$APP_DIR" remote get-url origin 2>/dev/null)"
else
    ok "$APP_DIR is free"
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^${APP_NAME}\.service"; then
    warn "a systemd unit named '${APP_NAME}' already exists — setup.sh would replace it"
else
    ok "systemd unit name '${APP_NAME}' is free"
fi

if id "$APP_USER" &>/dev/null; then
    warn "user '${APP_USER}' already exists (fine — it will be reused)"
else
    ok "user '${APP_USER}' is free"
fi

if [[ -e "/etc/nginx/sites-enabled/${APP_NAME}" ]]; then
    warn "an nginx site named '${APP_NAME}' is already enabled — setup.sh would replace it"
else
    ok "nginx site name '${APP_NAME}' is free"
fi

# ── Resources ──
hdr "Resources"
info "memory: $(free -h | awk '/^Mem:/{print $3 " used of " $2 ", " $7 " available"}')"
info "disk  : $(df -h / | awk 'NR==2{print $3 " used of " $2 ", " $4 " free"}')"
AVAIL_MB="$(free -m | awk '/^Mem:/{print $7}')"
if [[ "${AVAIL_MB:-0}" -lt 400 ]]; then
    warn "only ${AVAIL_MB}MB available — this app plus your existing services may be tight"
    info "each linked Telegram account holds an open MTProto connection"
fi

# ── Verdict ──
echo
if [[ "$CONFLICTS" -gt 0 ]]; then
    echo "${RED}${BLD}$CONFLICTS blocking issue(s) found.${OFF} Resolve them before running setup.sh."
    exit 1
fi
echo "${GRN}${BLD}No blocking conflicts.${OFF}"
echo "setup.sh will add only:"
echo "  - user '${APP_USER}', directory $APP_DIR"
echo "  - systemd unit '${APP_NAME}' on port ${FREE_PORT:-$WANT_PORT} (loopback only)"
echo "  - nginx site '${APP_NAME}' for ${DOMAIN:-your subdomain}"
echo
echo "Run setup.sh with the SAME app-name you passed here:"
echo "  sudo bash setup.sh ${DOMAIN:-your.domain.com} ${APP_NAME}"
echo "It will not modify other sites, other services, or your firewall's enabled state."
