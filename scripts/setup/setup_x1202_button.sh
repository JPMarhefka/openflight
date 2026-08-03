#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
LISTENER="$SCRIPT_DIR/x1202_button_listener.py"
SERVICE_PATH="/etc/systemd/system/openflight-x1202-button.service"
LOGIND_DROPIN_DIR="/etc/systemd/logind.conf.d"
LOGIND_DROPIN="$LOGIND_DROPIN_DIR/90-openflight-x1202-button.conf"
STATE_DIR="/var/lib/openflight-x1202-button"
PREVIOUS_LOGIND_DROPIN="$STATE_DIR/previous-logind-dropin"
SERVICE_NAME="openflight-x1202-button.service"
SUDOERS_PATH="/etc/sudoers.d/openflight-hotspot"
SIGMF_DIR="${OPENFLIGHT_SIGMF_DIR:-/home/admin/openflight_sessions/range-sigmf}"
TARGET_USER="${SUDO_USER:-${USER:-admin}}"

usage() {
    cat <<EOF
Usage:
  sudo $0 --diagnose
  sudo $0 --install --confirm
  sudo $0 --rollback

The installer changes power-button behavior only after --confirm.
EOF
}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

MODE=""
CONFIRM=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --diagnose|--install|--rollback)
            MODE="${1#--}"
            shift
            ;;
        --confirm)
            CONFIRM=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$MODE" ]; then
    usage >&2
    exit 2
fi

diagnose() {
    echo "X1202/Pi power-key devices:"
    if ! /usr/bin/python3 "$LISTENER" --discover; then
        echo "  No input device advertising KEY_POWER was found."
    fi
    echo
    echo "systemd-logind power-key settings:"
    /usr/bin/loginctl show-logind \
        -p HandlePowerKey -p HandlePowerKeyLongPress -p HandlePowerKeyIgnoreInhibited \
        2>/dev/null || true
    echo
    echo "Current OpenFlight logind drop-in:"
    if [ -f "$LOGIND_DROPIN" ]; then
        sed 's/^/  /' "$LOGIND_DROPIN"
    else
        echo "  none"
    fi
}

install_button() {
    if ! /usr/bin/python3 "$LISTENER" --discover; then
        echo "Refusing installation: no KEY_POWER device was detected." >&2
        exit 1
    fi
    if [ "$CONFIRM" != true ]; then
        echo "Refusing to change power-button behavior without --confirm." >&2
        echo "Review --diagnose output, then rerun with --install --confirm." >&2
        exit 1
    fi

    local target_uid
    target_uid=$(id -u "$TARGET_USER")
    install -d -m 0755 "$LOGIND_DROPIN_DIR"
    install -d -m 0700 "$STATE_DIR"
    if [ -f "$LOGIND_DROPIN" ] && ! grep -q "OpenFlight owns" "$LOGIND_DROPIN" && [ ! -f "$PREVIOUS_LOGIND_DROPIN" ]; then
        cp -p "$LOGIND_DROPIN" "$PREVIOUS_LOGIND_DROPIN"
    fi
    cat >"$LOGIND_DROPIN" <<'EOF'
# OpenFlight owns the X1202 power-button press while installed.
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
EOF

    cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=OpenFlight X1202 kiosk/hotspot button listener
After=systemd-logind.service graphical.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Environment=HOME=/home/$TARGET_USER
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$target_uid
ExecStart=/usr/bin/python3 "$LISTENER" --project-dir "$PROJECT_DIR" --sigmf-dir "$SIGMF_DIR"
Restart=always
RestartSec=2

[Install]
WantedBy=graphical.target
EOF

    cat >"$SUDOERS_PATH" <<EOF
# Allow the OpenFlight user to configure the fixed Launch_Dev NetworkManager hotspot.
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/nmcli
EOF
    chmod 0440 "$SUDOERS_PATH"
    if ! visudo -cf "$SUDOERS_PATH" >/dev/null; then
        rm -f "$SUDOERS_PATH"
        echo "Refusing installation: generated sudoers file failed validation." >&2
        exit 1
    fi

    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
    systemctl reload systemd-logind.service || true
    echo "Installed $SERVICE_NAME. A short X1202 button press starts kiosk with hotspot mode."
    echo "SigMF output: $SIGMF_DIR"
    echo "Rollback: sudo $0 --rollback"
}

rollback() {
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_PATH" "$SUDOERS_PATH"
    if [ -f "$PREVIOUS_LOGIND_DROPIN" ]; then
        cp -p "$PREVIOUS_LOGIND_DROPIN" "$LOGIND_DROPIN"
        rm -f "$PREVIOUS_LOGIND_DROPIN"
    else
        rm -f "$LOGIND_DROPIN"
    fi
    systemctl daemon-reload
    systemctl reload systemd-logind.service || true
    echo "Removed OpenFlight X1202 button integration."
    echo "The prior systemd-logind power-button behavior is restored."
}

case "$MODE" in
    diagnose) diagnose ;;
    install) install_button ;;
    rollback) rollback ;;
esac
