#!/usr/bin/env bash
# Optional background service (same as the "Install background service" button in the app).
# Keeps the software 90% charge limit and your saved fan curves active while the app is
# closed and across reboots.
#   ./service.sh install     install + start the service (also updates an older copy)
#   ./service.sh uninstall   remove the service and everything Legion Control put on the system
#   ./service.sh status
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=legion-control.service
UNIT=/etc/systemd/system/$NAME
OLD=legion-charge-limiter.service
LIBDIR=/usr/local/libexec/legion-control
CONF=/etc/legion-control
PY="$(command -v python3)"

case "${1:-}" in
  install)
    sudo install -Dm755 "$DIR/app/backend.py" "$LIBDIR/backend.py"
    sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Legion Control background service (90% charge limit, fan curves)

[Service]
ExecStart=$PY $LIBDIR/backend.py --daemon
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$NAME"
    sudo systemctl restart "$NAME"
    echo "Installed and started $NAME"
    ;;
  uninstall)
    for n in "$NAME" "$OLD"; do
      sudo systemctl disable --now "$n" 2>/dev/null || true
      sudo rm -f "/etc/systemd/system/$n"
    done
    sudo rm -rf "$LIBDIR" "$CONF"
    sudo systemctl daemon-reload
    echo "Removed the Legion Control service, $LIBDIR and $CONF"
    ;;
  status)
    systemctl status "$NAME" --no-pager || true
    ;;
  *)
    echo "usage: $0 install|uninstall|status" >&2
    exit 2
    ;;
esac
