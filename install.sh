#!/usr/bin/env bash
# Installs the thinkpad-powercap systemd unit + resume hook.
#   sudo bash ~/thinkpad-powercap/install.sh
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

CONF=/etc/thinkpad-powercap.conf
DEFAULTS=/etc/thinkpad-powercap.defaults
BIN=/usr/local/sbin/thinkpad-powercap
UNIT=/etc/systemd/system/thinkpad-powercap.service
HOOK=/usr/lib/systemd/system-sleep/thinkpad-powercap

# 1. Capture the CURRENT (factory) PL1/PL2 once, so we can always restore.
#    Skip if it already exists so a re-install can't save capped values as "default".
if [ ! -f "$DEFAULTS" ]; then
    d=/sys/class/powercap/intel-rapl:0
    {
        echo "# Factory RAPL limits captured at install time (microwatts)."
        echo "DEF_PL1_UW=$(cat "$d/constraint_0_power_limit_uw")"
        echo "DEF_PL2_UW=$(cat "$d/constraint_1_power_limit_uw")"
    } > "$DEFAULTS"
    echo "saved factory defaults -> $DEFAULTS"
fi

# 2. Install files (don't clobber an existing edited config).
[ -f "$CONF" ] || { install -m 0644 "$SRC/thinkpad-powercap.conf" "$CONF"; echo "installed $CONF"; }
install -m 0755 "$SRC/thinkpad-powercap"        "$BIN";  echo "installed $BIN"
install -m 0644 "$SRC/thinkpad-powercap.service" "$UNIT"; echo "installed $UNIT"
install -m 0755 "$SRC/thinkpad-powercap-sleep"  "$HOOK"; echo "installed $HOOK"

# 3. Enable + start.
systemctl daemon-reload
systemctl enable --now thinkpad-powercap
echo
echo "Done. Current limits:"
awk '{printf "  PL1=%dW  ", $1/1000000}' /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw
awk '{printf "PL2=%dW\n",  $1/1000000}' /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw
echo "Edit /etc/thinkpad-powercap.conf then: sudo systemctl restart thinkpad-powercap"
