# thinkpad-powercap

Caps the CPU package power on a ThinkPad (Intel Core Ultra / Meteor Lake) so the
firmware stops throttling the CPU to a crawl during long video calls and games.

## TL;DR

Install (persists across reboots and resume-from-suspend):

```bash
git clone https://github.com/michcio1234/thinkpad-powercap.git
cd thinkpad-powercap
sudo bash install.sh
thinkpad-powercap status      # check what's applied
```

**Effect:** the CPU package is held to a steady **22 W** (edit
`/etc/thinkpad-powercap.conf` to change). Under sustained load the die stays
around **~85 °C** instead of spiking to ~100 °C, so the firmware never kicks in
its emergency power cut — you get **steady performance with no periodic
freeze/stutter**, at the cost of a slightly lower peak clock.

Turn it off:

```bash
sudo bash uninstall.sh
```

## The problem it fixes

On these ThinkPads, during any *sustained* load — a Google Meet call with video,
a game, a benchmark — the machine runs fine for a minute or two, then the frame
rate / responsiveness suddenly collapses for ~1–2 minutes, then recovers, on
repeat. It isn't tied to anything you do in the app.

What's actually happening (confirmed by logging the CPU's own limit-reason
register, `MSR_CORE_PERF_LIMIT_REASONS`, alongside power and temperature):

1. The chip will draw 30–40 W when allowed, but the thin chassis can't cool that
   continuously, so the die climbs to **90–100 °C**.
2. Lenovo's firmware thermal manager (**DYTC**) reacts by **slashing the
   sustained power budget to ~10–12 W** to force the die to cool.
3. At ~10 W the CPU frequency collapses (to ~400–1500 MHz) — **that's the
   stutter** — and DYTC *holds* the low budget for ~1 minute even as the die
   rapidly cools to ~66 °C (which is why, mid-stutter, everything looks cold and
   the cause is non-obvious).
4. Budget restored → recovers → the die reheats → repeat.

Key findings that shaped the fix:
- It is **not** simple thermal throttling in the usual sense — during the stutter
  the die is *cool*. It's a **power-budget cut** triggered by earlier heat.
- The budget DYTC controls is the **MMIO** RAPL limit
  (`/sys/class/powercap/intel-rapl-mmio:0`), which is invisible in the normal
  MSR / `turbostat` view — the MSR package limit (64 W) is a red herring.
- The power profile only changes *when* it triggers, not *whether*: `balanced`
  sets the budget low from the start; `performance` runs free until the die
  actually gets hot, then gets cut the same way.

## How the fix works

Keep the die below the ~90 °C trigger so DYTC never invokes its cut.

Instead of chasing better cooling, we cap the *sustained package power* to a
level the cooler **can** dissipate. At **22 W** the die settles at ~85 °C under a
full 18-core load (hotter than any real call), the firmware budget stays at its
normal 40 W (no cut), and the CPU holds a steady clock — verified with
`verify_cap.sh`.

Two limits are capped (both configurable):

- **PL1** (sustained / long-term, ~28 s window) — the main lever.
- **PL2** (short burst / turbo) — capped too by default (`CAP_PL2=yes`).
  Capping only PL1 lets the chip burst to the factory ~64 W for ~28 s at the
  start of a load and spike the die to ~100 °C before PL1 settles it. Set
  `CAP_PL2=no` for snappier short tasks at the cost of that opening spike.

We write the **MSR** interface (`intel-rapl:0`), not MMIO: DYTC actively manages
MMIO and would fight a manual write, whereas it leaves MSR alone, and RAPL
enforces the most restrictive enabled limit — so a 22 W MSR cap binds below the
40 W MMIO budget.

The firmware resets these limits on every boot and on resume from suspend, so a
systemd service (boot) and a `system-sleep` hook (resume) re-apply them.

## Files

| File | Installs to | Purpose |
|------|-------------|---------|
| `install.sh` | — | install, capture factory defaults, enable + start |
| `uninstall.sh` | — | restore defaults, stop/disable, remove everything |
| `thinkpad-powercap` | `/usr/local/sbin/` | `apply` / `restore` / `status` |
| `thinkpad-powercap.conf` | `/etc/` | settings + inline reference/instructions |
| `thinkpad-powercap.service` | `/etc/systemd/system/` | apply at boot |
| `thinkpad-powercap-sleep` | `/usr/lib/systemd/system-sleep/` | re-apply on resume |

## Usage

```bash
thinkpad-powercap status                    # current caps, config, service state (no root)
sudo thinkpad-powercap apply                # apply cap from config (also run at boot/resume)
sudo thinkpad-powercap restore              # write factory limits back now

# change settings:
sudoedit /etc/thinkpad-powercap.conf        # set CAP_WATTS / CAP_PL2
sudo systemctl restart thinkpad-powercap    # apply the change
```

### Settings (`/etc/thinkpad-powercap.conf`)

- `CAP_WATTS` — sustained cap in watts (default 22; tune ~22–28).
- `CAP_PL2` — `yes` to also cap the burst limit (recommended), `no` to allow
  turbo bursts.

### Factory defaults (this machine)

MSR interface, `performance` profile: **PL1 = PL2 = 64 W**, nominal TDP 28 W.
Captured at install time to `/etc/thinkpad-powercap.defaults` so `restore` and
uninstall can put them back. A reboot also resets everything to firmware
defaults.

## Tuning

If 22 W feels too slow, raise `CAP_WATTS` (e.g. 26) and confirm the die stays
under ~90 °C under load:

```bash
sudo bash verify_cap.sh 26 120       # loads all cores, logs temp/power, prints a verdict
```

Watch for a live DYTC cut with `thinkpad-powercap status`: if the **MMIO PL1**
line drops to ~10–12 W under load, the die hit the trigger and the cap is too
high.
