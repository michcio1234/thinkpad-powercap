#!/usr/bin/env bash
# Uninstalls thinkpad-powercap: restores factory limits, stops/disables the
# unit, and removes all installed files.
#   sudo bash ~/thinkpad-powercap/uninstall.sh
set -u

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

CONF=/etc/thinkpad-powercap.conf
DEFAULTS=/etc/thinkpad-powercap.defaults
BIN=/usr/local/sbin/thinkpad-powercap
UNIT=/etc/systemd/system/thinkpad-powercap.service
HOOK=/usr/lib/systemd/system-sleep/thinkpad-powercap
PL1=/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw
PL2=/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw

# 1. Restore factory limits BEFORE removing the script/defaults it needs.
if [ -x "$BIN" ]; then
    "$BIN" restore && echo "restored factory limits"
else
    # Fallback: write saved defaults directly (or 64 W if the file is gone).
    DEF_PL1_UW=64000000; DEF_PL2_UW=64000000
    [ -r "$DEFAULTS" ] && . "$DEFAULTS"
    [ -w "$PL1" ] && echo "$DEF_PL1_UW" > "$PL1"
    [ -w "$PL2" ] && echo "$DEF_PL2_UW" > "$PL2"
    echo "restored factory limits (fallback)"
fi

# 2. Stop + disable the unit (ignore if already gone).
systemctl disable --now thinkpad-powercap 2>/dev/null && echo "disabled thinkpad-powercap.service" || true

# 3. Remove installed files.
for f in "$HOOK" "$UNIT" "$BIN" "$CONF" "$DEFAULTS"; do
    [ -e "$f" ] && { rm -f "$f" && echo "removed $f"; }
done

# 4. Reload systemd so the removed unit is forgotten.
systemctl daemon-reload

echo
echo "Uninstalled. Current limits:"
awk '{printf "  PL1=%dW  ", $1/1000000}' "$PL1" 2>/dev/null
awk '{printf "PL2=%dW\n",  $1/1000000}' "$PL2" 2>/dev/null || echo
echo "(These reset to firmware defaults on the next reboot regardless.)"
echo "The staging copies in ~/thinkpad-powercap/ are left untouched; delete manually if you want."
