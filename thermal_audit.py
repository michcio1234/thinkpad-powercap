#!/usr/bin/env python3
"""thermal_audit.py - correlate CPU frequency limiting with its cause.

Run as root (needs /dev/cpu/*/msr):
    sudo python3 ~/thermal_audit.py [interval_seconds] | tee ~/thermal_audit.log

Reproduce the stutter (Meet video / UNIGINE) while this runs, then look at the
ACTIVE column at the moment cur=...MHz collapses to ~400.

Captures in one timestamped stream:
  #1 MSR_CORE_PERF_LIMIT_REASONS (0x64F) - the CPU's own reason for limiting
  #2 every hwmon + thermal-zone temperature
  #3 power-supply / charger state
  #4 scaling_cur_freq vs scaling_max_freq (hardware vs software cap)
"""
import os
import sys
import glob
import math
import time
import struct

MSR_CORE_PERF_LIMIT_REASONS = 0x64F

# Bit -> name for the low 16 bits (active). The high 16 bits are the sticky
# "log" copy (set since boot/last clear). Layout per Intel client SDM.
#
# Only bits confirmed for Skylake-and-later client (0,1,5,6,10,11) are labelled
# plainly. Bits whose meaning/position varies by family are marked "?" because
# the diagnosis must not lean on a guess. Any *set* bit not in this dict is
# printed as "?bitN", and the raw hex is always logged, so the record can be
# re-decoded later. To pin the exact names on THIS CPU, cross-check with:
#     sudo turbostat --debug --show CoreTmp --interval 1   (prints decoded reasons)
# A bit that is always set under all-core load (commonly the Max/Multi-core
# Turbo Limit, around bit 14) is NORMAL, not a fault.
REASON_BITS = {
    0: "PROCHOT",
    1: "Thermal",
    4: "ResidencyReg?",
    5: "RATL(avg-therm)",
    6: "VR_ThermAlert",
    7: "VR_Current/TDC?",
    8: "Other/EDP?",
    10: "PL1(power)",
    11: "PL2(power)",
    # bits 12-14 (Turbo/EDP region) intentionally left unlabelled — they were
    # likely misassigned; they will surface as "?bitN" + raw hex instead.
}


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_indices():
    idx = []
    for p in glob.glob("/dev/cpu/*/msr"):
        parts = p.split("/")
        if len(parts) >= 4 and parts[3].isdigit():
            idx.append(int(parts[3]))
    return sorted(idx)


def read_msr(cpu, reg):
    fd = os.open("/dev/cpu/%d/msr" % cpu, os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, reg))[0]
    finally:
        os.close(fd)


def limit_reasons(cpus):
    """Union of active + sticky reason bits across all cores."""
    active = sticky = 0
    for c in cpus:
        try:
            v = read_msr(c, MSR_CORE_PERF_LIMIT_REASONS)
        except OSError:
            continue
        active |= v & 0xFFFF
        sticky |= (v >> 16) & 0xFFFF
    return active, sticky


def decode(mask):
    names = []
    for b in range(16):
        if (mask >> b) & 1:
            names.append(REASON_BITS.get(b, "?bit%d" % b))
    body = ",".join(names) if names else "-none-"
    return "0x%04x %s" % (mask, body)


def freqs():
    cur = [read_int(p) for p in glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    smax = [read_int(p) for p in glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq")]
    cur = [v for v in cur if v is not None]
    smax = [v for v in smax if v is not None]
    # -1 = unreadable (distinct from a real low frequency, which is never 0).
    return (max(cur) // 1000 if cur else -1,
            min(smax) // 1000 if smax else -1)


def hwmon_temps():
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read_str(os.path.join(h, "name")) or "?"
        for t in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            v = read_int(t)
            if v is None:
                continue
            label = read_str(t.replace("_input", "_label")) or os.path.basename(t)
            out.append("%s/%s=%dC" % (name, label, v // 1000))
    return out


def thermal_zones():
    out = []
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        typ = read_str(os.path.join(z, "type"))
        temp = read_int(os.path.join(z, "temp"))
        if typ is not None and temp is not None:
            out.append("%s=%dC" % (typ, temp // 1000))
    return out


def rapl_limits():
    """Current PL1 long_term limits from each interface (W), to see if the EC/
    DYTC moves them under load. MSR and MMIO are distinct interfaces; the
    firmware often enforces via MMIO while MSR shows the higher default."""
    out = []
    for d, tag in (("intel-rapl:0", "MSR_pkg"),
                   ("intel-rapl-mmio:0", "MMIO_pkg"),
                   ("intel-rapl:1", "PSys")):
        v = read_int("/sys/class/powercap/%s/constraint_0_power_limit_uw" % d)
        if v is not None:
            out.append("%s_PL1=%.1fW" % (tag, v / 1e6))
    return out


class PkgPower(object):
    """Package power (W) from the RAPL energy counter, across reads."""
    def __init__(self):
        self.path = "/sys/class/powercap/intel-rapl:0/energy_uj"
        self.wrap = read_int(
            "/sys/class/powercap/intel-rapl:0/max_energy_range_uj")
        self.prev_uj = read_int(self.path)
        self.prev_t = time.monotonic()

    def watts(self):
        uj = read_int(self.path)
        now = time.monotonic()
        if uj is None or self.prev_uj is None:
            self.prev_uj, self.prev_t = uj, now
            return -1.0
        duj = uj - self.prev_uj
        if duj < 0 and self.wrap:  # counter wrapped
            duj += self.wrap
        dt = now - self.prev_t
        self.prev_uj, self.prev_t = uj, now
        return (duj / 1e6) / dt if dt > 0 else -1.0


def power_state():
    out = []
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        n = os.path.basename(ps)
        typ = read_str(os.path.join(ps, "type")) or "?"
        online = read_str(os.path.join(ps, "online")) or "-"
        status = read_str(os.path.join(ps, "status")) or "-"
        # power_supply exposes these in micro-units; scale to W / A / V.
        extra = ""
        for fname, lab in (("power_now", "W"), ("current_now", "A"),
                           ("voltage_now", "V")):
            v = read_int(os.path.join(ps, fname))
            if v is not None:
                extra += " %s=%.2f" % (lab, v / 1e6)
        out.append("%s[%s] online=%s status=%s%s" % (n, typ, online, status, extra))
    return out


def main():
    interval = 1.0
    if len(sys.argv) > 1:
        try:
            interval = float(sys.argv[1])
        except ValueError:
            print("usage: thermal_audit.py [interval_seconds]", file=sys.stderr)
            sys.exit(2)
        if not math.isfinite(interval) or interval <= 0:
            print("interval must be a finite number > 0", file=sys.stderr)
            sys.exit(2)

    cpus = cpu_indices()
    if not cpus:
        print("ERROR: no /dev/cpu/*/msr found. Run: sudo modprobe msr",
              file=sys.stderr)
        sys.exit(1)
    try:
        read_msr(cpus[0], MSR_CORE_PERF_LIMIT_REASONS)
    except OSError as e:
        print("ERROR reading MSR 0x64F (run with sudo?): %s" % e, file=sys.stderr)
        sys.exit(1)

    full_every = 5  # dump full sensor/power block every N ticks
    print("# thermal_audit: %d cores, interval=%ss" % (len(cpus), interval))
    print("# Diagnose from ACTIVE (why it is limiting NOW). STICKY is "
          "cumulative since boot and saturates quickly - ignore for live cause.")
    print("# 1Hz polling can miss very brief throttle spikes; lower the "
          "interval to catch them. Full sensor+power block every %d ticks."
          % full_every)
    print("# time      cur/smax(MHz) pkgW | ACTIVE reasons | STICKY reasons")
    sys.stdout.flush()

    pkg = PkgPower()
    tick = 0
    try:
        while True:
            ts = time.strftime("%H:%M:%S")
            cur, smax = freqs()
            active, sticky = limit_reasons(cpus)
            watts = pkg.watts()
            print("%s  cur=%4d smax=%4d %5.1fW | ACTIVE: %-42s | STICKY: %s"
                  % (ts, cur, smax, watts, decode(active), decode(sticky)))
            if tick % full_every == 0:
                print("  [%s] RAPL:    %s" % (ts, "  ".join(rapl_limits())))
                print("  [%s] POWER:   %s" % (ts, " | ".join(power_state())))
                print("  [%s] THERMAL: %s" % (ts, "  ".join(thermal_zones())))
                print("  [%s] HWMON:   %s" % (ts, "  ".join(hwmon_temps())))
            sys.stdout.flush()
            tick += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
