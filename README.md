# iDRAC GPU Fan Control

Temperature-driven fan control for Dell PowerEdge servers (iDRAC7 / iDRAC8) running a consumer GPU passed through to a Proxmox VM.

iDRAC can't see a consumer GPU's temperature. This runs on the Proxmox host, reads the GPU temperature from inside the VM over SSH, reads the CPU temperature from iDRAC, and drives the server fans over IPMI, following whichever part needs more air.

See [CHANGELOG.md](CHANGELOG.md) for what changed in 2.0.

## Why

I put a Gigabyte RTX 3060 Gaming OC (triple fan) in a Dell R730. The only slot it fits puts the card's fans face-down into the PSU bay, so its own cooling barely works. The server's front-to-back fans are the real cooler.

The problem is that iDRAC has no idea the GPU exists:

- With Dell's third-party PCIe cooling response on, the fans run loud all the time.
- With it off, iDRAC idles the fans at ~10% while the GPU cooks. My first launch of Cyberpunk 2077 hit 86°C within minutes with the server fans at idle.

## How it works

Every 15 seconds, on the Proxmox host:

1. Read the CPU temperature from iDRAC (`ipmitool sdr type temperature`).
2. Ask Proxmox whether the VM is running (`qm status`).
3. If it is, SSH into the VM and read the GPU temperature (`nvidia-smi`).
4. Work out a fan speed for the GPU and one for the CPU, and take the higher.
5. Smooth it, then set all fans with Dell's IPMI raw commands.

| Situation | Fans |
|---|---|
| VM running | GPU curve or CPU curve, whichever is higher |
| VM stopped | 10% (the GPU is parked under vfio-pci and can't be read) |
| VM running, GPU unreadable (booting, sshd down, hung) | Hold current speed for 60 s, then fail safe to 25%. Never lowers the fans while blind |
| CPU temperature unreadable | Same fail-safe floor, logged once and again on recovery |
| Service stopped | Handed back to iDRAC automatic control (script trap + systemd `ExecStopPost`) |

Fan speeds are a percentage of max. On an R730, 60% is roughly as loud as POST.

### The curves

Both curves have the same four-point shape — floor, mid-point, target, absolute maximum — with linear interpolation between the anchors.

| | Floor | Mid | Target (`MAX_FAN`) | Absolute max |
|---|---|---|---|---|
| **GPU** | ≤35°C → 10% | 65°C → 35% | 70°C → 50% | ≥83°C → 100% |
| **CPU** | ≤35°C → 10% | 55°C → 15% | 75°C → 50% | ≥87°C → 100% |

`MAX_FAN` is the speed at the **target** temperature, not a ceiling. It's the loudest the fans get in normal operation, and under load the parts should settle at or below their target.

Above the target each curve keeps climbing to 100%, anchored to the hardware's real limit: 83°C is the RTX 3060's BIOS throttle point, 87°C is my iDRAC's CPU warning threshold. Earlier versions clipped at `MAX_FAN`, which meant that past 70°C the fans stopped responding to the GPU at exactly the point where responding mattered. Server fans are rated for 100% continuous duty — there's no reason to withhold range from a part that's getting hot.

In practice the top of the curve is rare. The hottest my card has run is 74°C in VR, which the curve answers with 65% — still a third of the range short of full. It takes 80°C to ask for 88%.

Check your own CPU thresholds with `ipmitool sensor | grep -E '^Temp'`. iDRAC reads the CPU die temperature, and its critical threshold is the CPU's DTS max. Set `CPU_MAX_TEMP` to your warning value, not your critical one.

### Smoothing

Fans go up immediately and come down gradually. Stepped curves make fans "hunt," bouncing between two speeds as the temperature crosses a bracket line. The deadband stops that: fans only step down once they're 3% or more above target.

The drop itself is proportional — `gap / 3`, with a floor of `STEP_DOWN` (2%). Near the target that floor dominates, so settling is gentle and unchanged from earlier versions. Over a large gap it closes much faster, which matters now that the curves reach 100%: a fixed 2% step took over eleven minutes to come down from the top, long after whatever caused it was over.

| From → to | Fixed 2% step | Proportional |
|---|---|---|
| 100% → 10% | 11.25 min | 2.50 min |
| 50% → 15% | 4.25 min | 1.75 min |
| 20% → 15% | 2 steps | 2 steps |

Because of the deadband the fans settle 1–2% above the target rather than exactly on it. That's intentional and inaudible.

### Logging

The script logs every fan speed change. It also writes a `[status]` line every 5 minutes and a `[temp]` line whenever either sensor moves 5°C since the last entry, so a steady system still produces a heartbeat.

That matters more than it sounds. An earlier version only logged on a change, which made a silent journal ambiguous — it could mean nothing had changed, or it could mean the service had been broken for weeks. With a heartbeat, silence means something is wrong.

### Why it never hands control back to iDRAC mid-load

An earlier version gave the fans back to iDRAC if the CPU got hot. That was a bad idea: iDRAC's automatic mode doesn't know the GPU exists, and it's comfortable running low fan speeds with a hot CPU. Handing back mid-game could lower the fans at exactly the moment the GPU needs them. The script keeps control and takes the higher of both curves instead.

## Results (my R730)

- **Before:** first launch of Cyberpunk 2077, 86°C within minutes, server fans at idle.
- **PassMark GPU test** (early version): 61°C peak, fans topped out at 30%.
- **Diablo IV, max settings, DLSS, no RT:** in the 70s on an earlier stepped curve; **66°C peak at 38% fans** on a later version.
- **VR (2.0, GPU uncapped at stock 170 W):** 74°C peak at 65% fans.
- **Idle with the VM running (2.0):** GPU ~36°C, fans ~18% — the CPU curve sets the idle speed, not the GPU.

**vfio-pci "VM off" measurements**, since people argue about this a lot:

- Whole-server power: 84 W with the VM running idle, 70 W with the VM stopped (the drop took a few minutes after shutdown).
- GPU temperature after ~14 minutes stopped, with iDRAC's idle fans: 48°C (read on the next VM boot).

My card parks fine under vfio-pci. Yours may not, and the fixed 10% while the VM is off gives it airflow either way.

## Tested on

- Dell PowerEdge R730, iDRAC8
- Dual Xeon E5-2697A v4 (16C/32T each, 145 W each)
- Gigabyte RTX 3060 Gaming OC 12GB, passed through to a Windows 11 Pro VM
- Proxmox VE (Debian trixie base)
- NVIDIA driver 621.14 in the VM
- Displays connected directly to the card (HDMI to a TV, DisplayPort for VR)

## Compatibility and limitations

- **iDRAC7 / iDRAC8 (12th / 13th gen PowerEdge):** the raw fan commands work. **iDRAC9** only accepts them up to firmware 3.30.30.30. Newer iDRAC9 firmware rejects them, and this script can't control your fans.
- One NVIDIA GPU in one VM. Written for a Windows VM; a Linux VM should work as-is if it has `nvidia-smi` and sshd with key auth, but that's untested.
- **NVIDIA driver 620.x and later report temperature as a float** (`43.19` instead of `43`). The script truncates it. If you are running a fork or an older copy of this script that validates the temperature with a plain integer check, it is silently broken on these drivers — see the CHANGELOG.
- **GPU core temperature only.** GDDR6 cards like the 3060 don't expose memory temperature, and memory runs hotter than the core. That's why the target is conservative.
- CPU temperature parsing expects Dell's sensor naming (`Temp` rows in `ipmitool sdr type temperature`). It takes the hottest CPU, so dual-socket works.
- Manual fan mode doesn't survive a reboot. iDRAC runs the fans during POST and boot until the service starts.
- The defaults are tuned for an uncapped RTX 3060 (stock 170 W) in an R730. Tune for your hardware.

## Requirements

- **Proxmox host:** root, `ipmitool`, the IPMI kernel modules
- **VM:** NVIDIA driver (provides `nvidia-smi`), OpenSSH Server

## Install

### 1. Proxmox host: IPMI and an SSH key

```bash
apt update && apt install -y ipmitool git
modprobe ipmi_devintf
modprobe ipmi_si
printf 'ipmi_devintf\nipmi_si\n' > /etc/modules-load.d/ipmi.conf
ipmitool sdr type temperature
```

You should see Inlet, Exhaust, and Temp readings. If not, stop here: IPMI isn't working.

```bash
ssh-keygen -t ed25519 -f /root/.ssh/gpu_fan_key -N ""
cat /root/.ssh/gpu_fan_key.pub
```

Copy the whole line it prints (it starts with `ssh-ed25519`).

### 2. Windows VM

Download `windows/setup-vm.ps1` from this repo inside the VM. Log in as the account the host will SSH in as, open PowerShell **as Administrator** in the folder with the script, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-vm.ps1 -PublicKey "ssh-ed25519 AAAA...your key... root@pve"
```

What it does:

- Installs and starts OpenSSH Server (the first install can take a few minutes) and opens port 22 if needed.
- Installs the host's key. Administrator accounts must use `C:\ProgramData\ssh\administrators_authorized_keys` with locked-down permissions, which is the most common Windows OpenSSH gotcha. The script handles it.
- `-PowerLimit` is a **legacy** option from 1.x, when I ran the card capped at 130 W. I no longer use it, and the 2.0 curves don't depend on it. It still works if you want a cap: add `-PowerLimit <watts>` and it sets the GPU power limit now and at every startup via a Scheduled Task. Check your card's allowed range first with `nvidia-smi -q -d POWER` (Min/Max Power Limit).
- Prints the VM's username and IP for the next step.

### 3. Back on the host: test

```bash
ssh -i /root/.ssh/gpu_fan_key -o StrictHostKeyChecking=no USER@VM-IP "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"
```

A number with no password prompt means it's working. On driver 620.x and later it will have a decimal (`43.19`); that's expected and the script handles it. A password prompt means the key didn't install; go back to step 2.

Test fan control (you'll hear it):

```bash
ipmitool raw 0x30 0x30 0x01 0x00        # manual mode
ipmitool raw 0x30 0x30 0x02 0xff 0x1e   # all fans to 30%
ipmitool raw 0x30 0x30 0x01 0x01        # back to automatic
```

### 4. Install the service

Find your VM's ID with `qm list`. Clone this repo (use the green **Code** button for the URL), then:

```bash
cd idrac-gpu-fan-control
install -m 755 gpu-fan-curve.sh /usr/local/bin/gpu-fan-curve.sh
install -m 644 gpu-fan-curve.service /etc/systemd/system/gpu-fan-curve.service
nano /usr/local/bin/gpu-fan-curve.sh
```

Set `VMID`, `VM_IP`, and `VM_USER` at the top. Save with `Ctrl+O`, `Enter`, `Ctrl+X`. Then:

```bash
systemctl daemon-reload
systemctl enable --now gpu-fan-curve.service
journalctl -u gpu-fan-curve.service -f
```

You should see the startup line listing both curves, then a `VM off` or `GPU=` line within 15 seconds. After that, a fan change, a `[temp]` line, or a `[status]` heartbeat at least every 5 minutes. If nothing appears for longer than that, something is wrong.

### Upgrading from 1.x

The tuning variables changed. Copy your `VMID`, `VM_IP` and `VM_USER` out of your running copy first, then:

```bash
install -m 755 gpu-fan-curve.sh /usr/local/bin/gpu-fan-curve.sh
nano /usr/local/bin/gpu-fan-curve.sh      # set the three values again
systemctl restart gpu-fan-curve.service
journalctl -u gpu-fan-curve.service -n 20
```

No `daemon-reload` is needed unless the unit file changed. If you had tuned `CPU_FULL`, `CPU_EMERG` or `EMERG_FAN`, read the CHANGELOG — the first was renamed and the last two are gone.

## Tuning

All settings are at the top of `gpu-fan-curve.sh`. Restart after editing: `systemctl restart gpu-fan-curve.service`

| Setting | Default | What it does |
|---|---|---|
| `INTERVAL` | 15 | Seconds between checks |
| `STATUS_INTERVAL` | 300 | Seconds between heartbeat log lines when fans are steady |
| `TEMP_LOG_DELTA` | 5 | Also log if either sensor moves this many °C since the last entry |
| `MIN_FAN` | 10 | Lowest active fan %. Keep it above your iDRAC's low-RPM warning threshold |
| `MAX_FAN` | 50 | Fan % at either curve's target temperature. The normal acoustic cap |
| `ABS_MAX_FAN` | 100 | Fan % at either curve's absolute maximum temperature |
| `GPU_MIN_TEMP` | 35 | GPU temperature at or below which fans sit at `MIN_FAN` |
| `KNEE` | 65 | GPU temperature where the curve turns steep (35%) |
| `GPU_GOAL` | 70 | GPU target temperature; fans reach `MAX_FAN` here. Lower = cooler and louder |
| `GPU_MAX_TEMP` | 83 | GPU temperature where fans reach `ABS_MAX_FAN`. Set to your card's throttle point |
| `CPU_MIN_TEMP` | 35 | CPU temperature at or below which fans sit at `MIN_FAN` |
| `CPU_START` | 55 | CPU temperature where the ramp accelerates above 15% |
| `CPU_GOAL` | 75 | CPU target temperature; fans reach `MAX_FAN` here |
| `CPU_MAX_TEMP` | 87 | CPU temperature where fans reach `ABS_MAX_FAN`. Set to your iDRAC CPU **warning** threshold |
| `DEADBAND` | 3 | Fans only come down once they're this far above target |
| `STEP_DOWN` | 2 | Minimum % dropped per check when settling near target |
| `OFF_FAN` | 10 | Fan % while the VM is stopped |
| `BLIND_FAN` | 25 | Fail-safe % when the VM is running but the GPU can't be read |
| `BLIND_GRACE` | 60 | Seconds to hold before failing safe (covers VM boot) |

The proportional ramp-down divisor is hardcoded as `gap / 3` in the main loop. Lower it for a faster descent.

## Useful commands

```bash
journalctl -u gpu-fan-curve.service -f    # live log
journalctl -u gpu-fan-curve.service -b    # this boot only
journalctl -u gpu-fan-curve.service | grep 'GPU='   # every temperature-driven change
ipmitool sdr type fan                      # fan RPMs
ipmitool sdr type temperature              # inlet / exhaust / CPU
ipmitool sensor | grep -E '^Temp'          # CPU warning / critical thresholds
ipmitool sdr | grep -i pwr                 # whole-server power draw
ipmitool raw 0x30 0x30 0x01 0x01           # hand the fans back to iDRAC right now
```

## Optional: Dell's third-party PCIe fan response

If iDRAC runs the fans loud whenever a non-Dell PCIe card is installed, this is why. While the script is running it doesn't matter, because manual mode overrides it, but it makes boot and any service downtime loud.

```bash
ipmitool raw 0x30 0xce 0x01 0x16 0x05 0x00 0x00 0x00                            # check current setting
ipmitool raw 0x30 0xce 0x00 0x16 0x05 0x00 0x00 0x00 0x05 0x00 0x01 0x00 0x00   # disable the response
```

## Uninstall

```bash
systemctl disable --now gpu-fan-curve.service
rm /etc/systemd/system/gpu-fan-curve.service /usr/local/bin/gpu-fan-curve.sh
systemctl daemon-reload
ipmitool raw 0x30 0x30 0x01 0x01
```

If you used the legacy `-PowerLimit` option, remove its task in the Windows VM with:

```powershell
Unregister-ScheduledTask -TaskName "GPU Power Limit" -Confirm:$false
```

## Safety

No warranty. This controls your server's cooling. What it does to fail safe:

- The script hands the fans back to iDRAC automatic control on exit, including on `Ctrl+C` if you run it by hand. The systemd unit's `ExecStopPost` does the same as a second layer.
- If the VM is running but the GPU can't be read, it holds the current speed for 60 seconds, then goes to `BLIND_FAN`. It never lowers the fans while blind.
- If Proxmox can't report the VM's state, that's treated the same as blind.
- If the CPU sensor becomes unreadable, the CPU vote holds a safe floor rather than reading as 0°C.
- If an IPMI write fails, the script does not update its internal state and retries on the next check. The failure and the recovery are both logged.
- Both curves run to 100% before their respective hardware limits, so the fans are never withholding range from a part that's overheating.
- Missing dependencies make the script exit before it touches the fans. iDRAC stays in automatic mode, and the journal tells you what's wrong.

Behind all of that, both the CPU and GPU throttle and ultimately shut themselves down at their own limits regardless of fan speed. Watch the journal for the first few days anyway.

## About this project

I'm not a coder, and AI wrote the code. Version 1.0 took about a week, mostly with Claude (Anthropic), with other AIs helping. The 2.0 rewrite used Claude, Grok (xAI) and Gemini (Google), and no single one of them did most of it. My part was the hardware, the testing, the measurements, and never trusting any of it: I pushed back whenever an answer didn't match what the server actually did, which happened more than once. Everything described here was tested on my R730. Read the script before you run it: it's short, commented, and it controls your server's cooling.

## License

MIT. See [LICENSE](LICENSE).
