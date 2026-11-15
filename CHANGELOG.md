# Changelog

All notable changes to this project.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.0.0] - 2026-11-15

Rewrite of the control logic. The fan curves, the ramp-down, the logging and the
failure handling all changed. If you are upgrading, re-read the Tuning table in
the README — several variables were renamed or removed.

### Fixed

- **NVIDIA driver 620.x / 621.x broke temperature parsing.** `nvidia-smi
  --query-gpu=temperature.gpu` now returns a float (`43.19`) where it previously
  returned an integer (`43`). The validation check `[[ "$GPU_TEMP" =~ ^[0-9]+$ ]]`
  rejected it, so every poll fell through to the blind path and the fans pinned at
  `BLIND_FAN` regardless of actual GPU temperature.

  This failed silently in both directions: the old script only logged on a fan
  speed change, and since the target never changed, nothing was ever written to the
  journal. The service looked healthy and the fans sat at a plausible speed.

  Fixed by truncating the decimal before validation:

  ```bash
  RAW_GPU=${RAW_GPU%%.*}   # driver 620/621 emits floats (43.19) -> 43
  ```

  `${var%%.*}` is a no-op on integer input, so it is safe on older drivers and if
  NVIDIA reverts the change.

- **Fans could strand above the floor.** With a deadband of 3 and a step of 2, a
  target at or near the minimum was never reached — the gap stayed under the
  deadband and the fans held 2–3% high indefinitely. The step-down now bypasses the
  deadband once the target is at the floor.

- **Fan speed could desync from reality.** `set_fans` ignored ipmitool's exit
  status and the script updated its internal `CURRENT` regardless. A failed IPMI
  write left the script believing it had set a speed it had not, and it never
  retried. `set_fans` now returns a status, `CURRENT` only advances on success, and
  the first failure and the recovery are both logged.

- **Nothing set the fans during the first 60 seconds if the GPU was unreadable at
  startup.** The CPU vote required `CURRENT >= 0`, which is false before the first
  successful write, so the whole grace period ran with iDRAC still in control. The
  CPU now sets the opening speed.

### Added

- **Full fan range above the acoustic cap.** The GPU curve previously clipped at
  `MAX_FAN` (50%), reached at 70°C. Every temperature above that produced an
  identical fan speed, so the fans stopped responding to the card exactly where
  responding mattered. Both curves now continue past the cap to 100%, anchored to
  the hardware's real limits: 83°C for the GPU (the RTX 3060's BIOS throttle point)
  and 87°C for the CPU (iDRAC's warning threshold).

  `MAX_FAN` is now what it always meant — the speed at the *target* temperature,
  not a hard ceiling. `ABS_MAX_FAN` is the ceiling.

- **Periodic and delta logging.** The script previously logged only on a fan speed
  change, which meant a silent journal was ambiguous: it could mean nothing had
  changed, or it could mean the service was dead. It now emits a `[status]` line
  every `STATUS_INTERVAL` seconds (default 300) and a `[temp]` line whenever either
  sensor moves `TEMP_LOG_DELTA` degrees (default 5) since the last entry. A silent
  journal now means something is wrong.

- **`ssh` added to the startup dependency check.** 1.0 already checked for
  `ipmitool` and `qm`; the script now also verifies `ssh` and exits with a clear
  `FATAL` line before touching the fans if any are missing.

- **iDRAC restore on exit, in the script itself.** An `EXIT`/`INT`/`TERM` trap hands
  the fans back to iDRAC automatic control. This previously relied entirely on the
  systemd unit's `ExecStopPost`, which does nothing if you run the script by hand.
  The unit's `ExecStopPost` is kept as a second layer.

  Bash won't run a trap while a foreground `sleep` is in progress, so the poll
  sleep is backgrounded and waited on. That keeps a stop from waiting out the rest
  of the 15-second interval before the fans are handed back:

  ```bash
  sleep "$INTERVAL" &
  wait $!
  ```

- **CPU sensor failure handling.** An unreadable CPU temperature is now detected
  rather than silently treated as 0°C. The script holds a safe floor, logs the
  failure once, and logs again on recovery.

### Changed

- **Both curves are now 4-point and symmetric.** Previously the GPU had a two-slope
  curve and the CPU had a linear ramp plus a separate emergency tier. Both now use
  the same shape: floor, mid-point, target, absolute maximum — with linear
  interpolation between anchors.

  | GPU temp | Fans | | CPU temp | Fans |
  |---|---|---|---|---|
  | ≤35°C | 10% | | ≤35°C | 10% |
  | 65°C (knee) | 35% | | 55°C | 15% |
  | 70°C (goal) | 50% | | 75°C (goal) | 50% |
  | ≥83°C | 100% | | ≥87°C | 100% |

- **Ramp-down is proportional instead of a fixed step.** The old fixed 2% per check
  was fine against a 50% ceiling but produced an 11-minute tail from 100%. The drop
  is now `gap / 3`, floored at `STEP_DOWN`, so large gaps close quickly while
  behaviour near the target is unchanged.

  | From → to | Before | After |
  |---|---|---|
  | 100% → 10% | 11.25 min | 2.50 min |
  | 88% → 15% | 9.00 min | 2.25 min |
  | 50% → 15% | 4.25 min | 1.75 min |
  | 20% → 15% | 2 steps | 2 steps (unchanged) |

- `OFF_FAN` lowered from 15% to 10%.
- `BLIND_FAN` lowered from 40% to 25%.
- `CPU_FULL` renamed to `CPU_GOAL`, for symmetry with `GPU_GOAL`.
- **GPU power limit is now documented as legacy.** The author's card runs
  uncapped at its stock 170 W. `windows/setup-vm.ps1 -PowerLimit` still works and
  is unchanged, but the README no longer recommends it and the 2.0 curves don't
  depend on it.

### Removed

- **The CPU emergency tier** (`CPU_EMERG`, `EMERG_FAN`). It was a flat 60% that
  fired at 80°C and short-circuited the rest of the loop, bypassing the curve, the
  deadband and the GPU vote. Extending the CPU curve to 100% at 87°C does the same
  job continuously, with no second control path that can hijack the loop — and it
  is more aggressive, since the curve asks for 70% at 80°C where the old tier asked
  for 60%.

### Known issues

- 1.0 refused to start if `VMID`, `VM_IP` or `VM_USER` was still `CHANGE_ME`, or if
  the SSH key was missing. 2.0 doesn't check for these. If you forget to set them,
  the GPU will read as unreadable and the fans will run at `BLIND_FAN` (or higher)
  instead of the script stopping with an error. Check the journal after install.

## [1.0.0] - 2026-09-30

Initial public release.

- GPU curve driven by `nvidia-smi` over SSH from inside the passthrough VM.
- CPU curve driven by `ipmitool` on the Proxmox host.
- Fans follow whichever of the two wants more air.
- Fixed-step smoothing on the way down, immediate response on the way up.
- VM-aware: separate speeds for VM stopped and for GPU unreadable.
- CPU emergency tier at 80°C.
- systemd unit with `ExecStopPost` handing fans back to iDRAC.
- Windows VM setup script (`windows/setup-vm.ps1`) for OpenSSH, key install and
  GPU power limit.
