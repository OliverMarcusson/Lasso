#!/bin/bash
# Remove everything setup-network.sh created. Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

nmcli connection delete lasso0 2>/dev/null || true
systemctl disable --now lasso-nftables.service 2>/dev/null || true
nft destroy table inet lasso
rm -f /etc/systemd/system/lasso-nftables.service /etc/lasso/lasso.nft
rmdir /etc/lasso 2>/dev/null || true
systemctl daemon-reload
echo "lasso0 removed"
