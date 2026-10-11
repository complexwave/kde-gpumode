#!/bin/sh
# gpumode installer. Run as root:  sudo ./install.sh   |   sudo ./install.sh uninstall
# Re-run after pulling updates. /etc/gpumode.conf is never overwritten.
set -e
cd "$(dirname "$0")"

LIB=/usr/local/lib/gpumode
BIN=/usr/local/bin/gpumode
CONF=/etc/gpumode.conf
SYS_UNIT=/etc/systemd/system/gpumoded.service
USER_UNIT=/etc/systemd/user/gpumode.service

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }

# run systemctl --user for the desktop user who invoked sudo, if we can tell who that is
user_systemctl() {
    [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != root ] || return 1
    uid=$(id -u "$SUDO_USER")
    [ -d "/run/user/$uid" ] || return 1
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user "$@"
}

if [ "$1" = uninstall ]; then
    user_systemctl stop gpumode.service 2>/dev/null || true
    systemctl --global disable gpumode.service 2>/dev/null || true
    systemctl disable --now gpumoded.service 2>/dev/null || true
    rm -f "$SYS_UNIT" "$USER_UNIT" "$BIN"
    rm -rf "$LIB"
    systemctl daemon-reload
    echo "removed. Left in place: $CONF, /var/lib/gpumode"
    exit 0
fi

python3 -c 'import pynvml' 2>/dev/null || {
    echo "missing python module pynvml: apt install python3-pynvml  (or: pip install nvidia-ml-py)" >&2
    exit 1
}
python3 -c 'import PyQt5.QtWidgets' 2>/dev/null ||
    echo "warning: PyQt5 missing, the widget will not start: apt install python3-pyqt5" >&2

install -D -m 644 gpumoded.py "$LIB/gpumoded.py"
install -m 644 gpumode.py "$LIB/gpumode.py"
install -m 644 gpumoded.service "$SYS_UNIT"
install -D -m 644 gpumode.service "$USER_UNIT"
printf '#!/bin/sh\nexec /usr/bin/python3 %s/gpumode.py "$@"\n' "$LIB" > "$BIN"
chmod 755 "$BIN"

if [ ! -e "$CONF" ]; then
    python3 "$LIB/gpumoded.py" --default-config > "$CONF"
    echo "wrote starter config $CONF  (edit it, then: systemctl reload gpumoded)"
fi
python3 "$LIB/gpumoded.py" --check "$CONF"

systemctl daemon-reload
systemctl enable gpumoded.service
systemctl restart gpumoded.service
systemctl --global enable gpumode.service

if user_systemctl daemon-reload; then
    user_systemctl restart gpumode.service && echo "widget started for $SUDO_USER"
else
    echo "widget starts at next graphical login; to start it now run as your desktop user:"
    echo "  systemctl --user daemon-reload && systemctl --user start gpumode"
fi
