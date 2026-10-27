#!/bin/bash
# Set up the Lasso catch-all network (lasso0) for one user. Run as root. Idempotent.
#
#   setup-network.sh --user NAME
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
user=""
while [ $# -gt 0 ]; do
	case "$1" in
	--user) user=$2; shift 2 ;;
	*) echo "usage: $0 --user NAME" >&2; exit 2 ;;
	esac
done
[ -n "$user" ] || { echo "usage: $0 --user NAME" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
uid=$(id -u "$user")

# Firewall: rules file plus a unit that loads it at boot.
install -d -m 755 /etc/lasso
sed "s/@UID@/$uid/" "$here/lasso.nft.in" >/etc/lasso/lasso.nft
chmod 644 /etc/lasso/lasso.nft
install -m 644 "$here/lasso-nftables.service" /etc/systemd/system/lasso-nftables.service
systemctl daemon-reload
systemctl enable lasso-nftables.service
systemctl restart lasso-nftables.service

# NetworkManager tun connection. dns-priority 1000 (worse than the default 100) means
# NetworkManager gives the "~." routing domain to any real connection with a default route
# instead of lasso0, so resolved only sends queries to 10.211.0.1 while lasso0 is the only
# link with a default route. See README.md.
settings=(
	connection.interface-name lasso0
	connection.autoconnect yes
	connection.llmnr no
	connection.mdns no
	tun.mode 1
	tun.owner "$uid"
	tun.pi no
	tun.multi-queue no
	ipv4.method manual
	ipv4.addresses 10.211.0.2/30
	ipv4.gateway 10.211.0.1
	ipv4.route-metric 30000
	ipv4.never-default no
	ipv4.dns 10.211.0.1
	ipv4.dns-priority 1000
	ipv6.method manual
	ipv6.addresses fd6c:6173:736f::2/64
	ipv6.gateway fd6c:6173:736f::1
	ipv6.route-metric 30000
	ipv6.never-default no
	ipv6.dns-priority 1000
)
if nmcli -t -f NAME connection show | grep -qx lasso0; then
	nmcli connection modify lasso0 "${settings[@]}"
else
	nmcli connection add type tun con-name lasso0 "${settings[@]}"
fi
nmcli connection up lasso0 || echo "warning: lasso0 did not come up yet; it will autoconnect" >&2

echo "lasso0 ready for $user (uid $uid)"
