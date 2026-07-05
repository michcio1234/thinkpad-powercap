#!/usr/bin/env bash
# Verify a package power cap keeps the die off the DYTC cut threshold.
#
#   sudo bash ~/verify_cap.sh [cap_watts=22] [duration_secs=120]
#
# Sets MSR PL1 to the cap, drives all cores to heat the die, logs with
# thermal_audit.py, then restores the original limit and prints a verdict.
# The MSR interface is used on purpose: DYTC manages the MMIO limit, not MSR,
# so an MSR cap won't be fought — and RAPL enforces the most restrictive
# enabled limit, so a 22 W MSR cap binds below the 40 W MMIO budget.
set -u

CAP_W=${1:-22}
DUR=${2:-120}
CAP_UW=$((CAP_W * 1000000))
PL1=/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw  # sustained
PL2=/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw  # short burst
AUDIT="$(cd "$(dirname "$0")" && pwd)/thermal_audit.py"
LOG=./thermal_audit_cap${CAP_W}.log

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi
[ -w "$PL1" ] && [ -w "$PL2" ] || { echo "Cannot write RAPL limits"; exit 1; }
[ -f "$AUDIT" ] || { echo "Missing $AUDIT"; exit 1; }

# Cap BOTH PL1 and PL2: PL1 alone lets the chip burst at PL2 (64W) for the
# ~28s averaging window, which spikes the die to 100C before PL1 clamps.
ORIG1=$(cat "$PL1"); ORIG2=$(cat "$PL2")
LOAD_PIDS=()
stop_load() { for p in "${LOAD_PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; LOAD_PIDS=(); }
cleanup() {
  stop_load
  echo "$ORIG1" > "$PL1" 2>/dev/null; echo "$ORIG2" > "$PL2" 2>/dev/null
  echo "[restored MSR PL1=$((ORIG1 / 1000000))W PL2=$((ORIG2 / 1000000))W]"
}
trap cleanup EXIT INT TERM

echo "$CAP_UW" > "$PL1"; echo "$CAP_UW" > "$PL2"
echo "[set MSR PL1=PL2=${CAP_W}W (was PL1=$((ORIG1 / 1000000))W PL2=$((ORIG2 / 1000000))W); profile=$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)]"

N=$(nproc)
echo "[starting $N busy-loop workers to heat the die for ${DUR}s]"
for _ in $(seq 1 "$N"); do
  python3 -c 'while True: pass' & LOAD_PIDS+=($!)
done

timeout "${DUR}s" python3 "$AUDIT" 1 | tee "$LOG"
stop_load

echo
echo "================= VERDICT (cap ${CAP_W}W) ================="
maxt=$(grep 'THERMAL:' "$LOG" | grep -oE 'x86_pkg_temp=[0-9]+' | sed 's/.*=//' | sort -n | tail -1)
mmio_min=$(grep 'RAPL:' "$LOG" | grep -oE 'MMIO_pkg_PL1=[0-9.]+' | sed 's/.*=//' | sort -n | head -1)
mmio_max=$(grep 'RAPL:' "$LOG" | grep -oE 'MMIO_pkg_PL1=[0-9.]+' | sed 's/.*=//' | sort -n | tail -1)
pkgw=$(grep -E 'cur=' "$LOG" | grep -oE '[0-9.]+W \| ACTIVE' | grep -oE '^[0-9.]+' \
        | awk '{s+=$1;n++} END{if(n)printf "%.1f", s/n; else print "n/a"}')
mincur=$(grep -E 'cur=' "$LOG" | grep -oE 'cur=[ 0-9-]+' | sed 's/cur=//' \
        | awk '{print $1}' | sort -n | head -1)
echo "max die temp (x86_pkg):     ${maxt}C      -> PASS if < 90"
echo "MMIO PL1 range during run:  ${mmio_min}..${mmio_max}W  -> PASS if it never drops to ~10-12 (no DYTC cut)"
echo "avg package power:          ${pkgw}W    -> confirms the MSR cap binds (want ~${CAP_W})"
echo "min CPU freq:               ${mincur}MHz -> should be steady, not collapsing 400<->2000"
echo
echo "Interpretation: PL1(power) being ACTIVE is EXPECTED here (we are deliberately"
echo "holding ${CAP_W}W). The win is a cool die + MMIO staying at its baseline (no"
echo "40->12W emergency cut) + steady freq, instead of the heat->cut->stutter cycle."
echo "==========================================================="
