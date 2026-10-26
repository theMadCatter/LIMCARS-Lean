#!/usr/bin/env bash
# Install LIMCARS Lean as a per-user systemd service.
#
# Why this script exists:
#   The old scripts/limcars-next@.service shipped with a hardcoded path of
#   /home/%i/projects/limcars-next that nobody but the original maintainer
#   had. Users following the README copied the unit in, got
#   "status=203/EXEC", and were left debugging path mismatches.
#
# What it does:
#   * computes the repo's absolute path from this script's location
#   * copies scripts/limcars-user.service to ~/.config/systemd/user/
#     substituting the real path, so there is nothing for the user to edit
#   * enables + starts the service, and prints the exact journalctl command
#     to tail it with
#
# Usage:
#   bash scripts/install-user-systemd.sh
#     (optional) LIMCARS_HOST=0.0.0.0 LIMCARS_TOKEN=abc bash scripts/install-user-systemd.sh
#   — any LIMCARS_* env vars present at install time are copied into an
#   `Environment=` drop-in so the service inherits them on every start.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/limcars.service"

if [[ ! -f "$REPO/scripts/start.sh" ]]; then
  echo "[limcars] install aborted: scripts/start.sh not found under $REPO" >&2
  exit 2
fi

mkdir -p "$UNIT_DIR"
# Substitute the placeholder with the real repo path. sed is portable; this
# avoids anything systemd-specific that could change between distros.
sed "s|__LIMCARS_DIR__|$REPO|g" "$REPO/scripts/limcars-user.service" > "$UNIT_FILE"

# Preserve any LIMCARS_* vars from the installing shell so the service
# inherits them. The drop-in sits NEXT to the unit — removing it does not
# break the unit itself.
DROPIN_DIR="$UNIT_DIR/limcars.service.d"
DROPIN="$DROPIN_DIR/env.conf"
LIMCARS_ENV=$(env | awk -F= '/^LIMCARS_/ {print}')
if [[ -n "$LIMCARS_ENV" ]]; then
  mkdir -p "$DROPIN_DIR"
  {
    echo "[Service]"
    while IFS= read -r line; do
      echo "Environment=$line"
    done <<< "$LIMCARS_ENV"
  } > "$DROPIN"
  echo "[limcars] wrote $DROPIN with:"
  sed 's/^/[limcars]   /' "$DROPIN"
fi

systemctl --user daemon-reload
systemctl --user enable --now limcars.service
# Allow the service to keep running after the user logs out. Non-fatal if
# `loginctl` isn't available (e.g. inside a container).
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

cat <<EOF

[limcars] installed as a user service.
          unit    : $UNIT_FILE
          status  : systemctl --user status limcars.service
          logs    : journalctl --user -u limcars.service -f
          disable : systemctl --user disable --now limcars.service
EOF
