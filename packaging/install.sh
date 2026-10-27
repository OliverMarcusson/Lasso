#!/usr/bin/env bash
# Install or remove the Lasso laptop package and the lassod user service.
#
#   packaging/install.sh              install or update
#   packaging/install.sh --uninstall  stop lassod, remove its unit and the uv tool
#
# Run as your normal user. Nothing here uses sudo; the network setup needs root,
# so its command is printed at the end for you to run.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LAPTOP_DIR="$REPO_DIR/laptop"
NET_DIR="$REPO_DIR/packaging/network"
UNIT_SRC="$SCRIPT_DIR/systemd/lassod.service"
UNIT_NAME="lassod.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/$UNIT_NAME"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename -- "$0") [--uninstall]

  (no args)     install or update the uv tool and the lassod user service
  --uninstall   stop and disable lassod, remove its unit, uninstall the uv tool
EOF
}

# The uv tool name is the [project] name in laptop/pyproject.toml (falls back to "lasso").
project_name() {
  local name=""
  if [[ -f "$LAPTOP_DIR/pyproject.toml" ]]; then
    name="$(awk -F'"' '/^name = /{print $2; exit}' "$LAPTOP_DIR/pyproject.toml")"
  fi
  printf '%s\n' "${name:-lasso}"
}

TOOL_NAME="$(project_name)"

require_user() {
  if [[ $EUID -eq 0 ]]; then
    die "run as your normal user, not root (only the network step needs sudo)"
  fi
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
  systemctl --user show-environment >/dev/null 2>&1 \
    || die "no systemd user manager reachable; run this from your logged-in session"
}

# True if uv has a tool installed under exactly the name $1.
tool_installed() {
  uv tool list 2>/dev/null | awk -v n="$1" '$1 == n { found = 1 } END { exit !found }'
}

do_install() {
  require_user
  command -v uv >/dev/null 2>&1 || die "uv not found in PATH (https://docs.astral.sh/uv/)"
  [[ -f "$LAPTOP_DIR/pyproject.toml" ]] || die "missing $LAPTOP_DIR/pyproject.toml"
  [[ -f "$UNIT_SRC" ]] || die "missing $UNIT_SRC"
  command -v notify-send >/dev/null 2>&1 \
    || warn "notify-send not found (libnotify); lassod will run but cannot show notifications"

  info "installing uv tool '$TOOL_NAME' from $LAPTOP_DIR"
  uv tool install --force --reinstall "$LAPTOP_DIR"

  for bin in lasso lassod; do
    [[ -x "$HOME/.local/bin/$bin" ]] \
      || die "$HOME/.local/bin/$bin not found after install (the unit's ExecStart expects it there)"
  done

  info "installing unit to $UNIT_DST"
  install -d -m 0755 "$UNIT_DIR"
  if [[ -f "$UNIT_DST" ]] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    echo "unit unchanged"
  else
    install -m 0644 "$UNIT_SRC" "$UNIT_DST"
    echo "unit written"
  fi
  systemctl --user daemon-reload

  info "enabling $UNIT_NAME"
  local was_active=0
  if systemctl --user is-active --quiet "$UNIT_NAME"; then
    was_active=1
  fi
  systemctl --user enable --now "$UNIT_NAME"
  if (( was_active )); then
    # enable --now leaves a running instance alone; restart so it picks up the new binary.
    systemctl --user restart "$UNIT_NAME"
  fi
  echo "$UNIT_NAME is $(systemctl --user is-active "$UNIT_NAME" || true)"

  local user_name
  user_name="$(id -un)"
  cat <<EOF

Done. Manual steps left:

1. Network setup (needs root, so run it yourself):
     cd "$NET_DIR"
     sudo ./setup-network.sh --user $user_name
   Add --deny-unit UNIT for units whose traffic should not count as demand.
   Details: packaging/network/README.md

2. Pairing: open the pairing screen on the phone first, then:
     lasso scan
     lasso pair <address>
EOF
}

do_uninstall() {
  require_user

  if [[ -f "$UNIT_DST" ]]; then
    info "stopping and disabling $UNIT_NAME"
    systemctl --user disable --now "$UNIT_NAME" \
      || warn "could not disable $UNIT_NAME; check 'systemctl --user status $UNIT_NAME'"
    rm -f "$UNIT_DST"
    systemctl --user daemon-reload
    echo "removed $UNIT_DST"
  else
    echo "no unit at $UNIT_DST; nothing to remove"
  fi

  if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found; skipping uv tool uninstall"
  elif tool_installed "$TOOL_NAME"; then
    info "uninstalling uv tool '$TOOL_NAME'"
    uv tool uninstall "$TOOL_NAME"
  else
    echo "uv tool '$TOOL_NAME' not installed; nothing to remove"
  fi

  cat <<EOF

Not removed by this script:
  - network setup, if you ran it:  cd "$NET_DIR" && sudo ./remove-network.sh
  - config and state under ~/.config/lasso
EOF
}

if (( $# > 1 )); then
  usage >&2
  exit 2
fi

case "${1:-}" in
  "")          do_install ;;
  --uninstall) do_uninstall ;;
  -h|--help)   usage ;;
  *)           usage >&2; exit 2 ;;
esac
