#!/usr/bin/env bash
# Install Zaliver + Antidetect nginx reverse-proxy config.
#
# Usage:
#   sudo bash scripts/nginx/install-zaliver-proxy.sh
#   sudo bash scripts/nginx/install-zaliver-proxy.sh /path/to/custom.conf
#
# Expects Debian/Ubuntu-style layout (/etc/nginx/sites-available|sites-enabled).
# On RHEL/CentOS/Alma set NGINX_CONF_D=1 to install into /etc/nginx/conf.d/

set -euo pipefail

SITE_NAME="zaliver-proxy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CONF="${1:-${SCRIPT_DIR}/zaliver-proxy.conf}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

if [[ ! -f "$SRC_CONF" ]]; then
  echo "Config not found: $SRC_CONF" >&2
  exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx not found. Install first, e.g.: apt install nginx" >&2
  exit 1
fi

# Warn if placeholders are still present
if grep -qE 'zaliver\.example\.com|antidetect\.example\.com' "$SRC_CONF"; then
  echo "WARNING: config still has example.com domains — edit server_name before going live."
  echo "  file: $SRC_CONF"
  echo
fi

if [[ "${NGINX_CONF_D:-0}" == "1" ]] || [[ ! -d /etc/nginx/sites-available ]]; then
  DEST="/etc/nginx/conf.d/${SITE_NAME}.conf"
  echo "Installing to $DEST"
  cp -f "$SRC_CONF" "$DEST"
else
  DEST_AVAIL="/etc/nginx/sites-available/${SITE_NAME}.conf"
  DEST_ENABLED="/etc/nginx/sites-enabled/${SITE_NAME}.conf"
  echo "Installing to $DEST_AVAIL"
  cp -f "$SRC_CONF" "$DEST_AVAIL"
  ln -sfn "$DEST_AVAIL" "$DEST_ENABLED"
  # Drop default site if it steals port 80
  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    echo "Disabling default site (was occupying :80)"
    rm -f /etc/nginx/sites-enabled/default
  fi
fi

echo "Testing nginx config..."
nginx -t

echo "Reloading nginx..."
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
  systemctl reload nginx
elif command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now nginx
else
  nginx -s reload || service nginx reload
fi

echo
echo "OK. Proxy is live (HTTP :80):"
echo "  Host: zaliver.*     -> 127.0.0.1:8080"
echo "  Host: antidetect.*  -> 127.0.0.1:18765"
echo
echo "Next:"
echo "  1. Edit domains in the installed conf (server_name)"
echo "  2. Point DNS A-records to this server IP"
echo "  3. Ensure backends listen locally:"
echo "       Zaliver:     ZALIVER_API_HOST=127.0.0.1 ZALIVER_API_PORT=8080"
echo "       Antidetect:  http://127.0.0.1:18765"
echo "  4. Optional TLS: certbot --nginx -d your.domain"
