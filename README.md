# iDRAC GPU Fan Control

Temperature-driven fan control for Dell PowerEdge servers (iDRAC7 / iDRAC8) running a consumer GPU passed through to a Proxmox VM.

iDRAC can't see a consumer GPU's temperature. This runs on the Proxmox host, reads the GPU temperature from inside the VM over SSH, reads the CPU temperature from iDRAC, and drives the server fans over IPMI, following whichever part needs more air.

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
| VM stopped | 15% (the GPU is parked under vfio-pci and can't be read) |
| VM running, GPU unreadable (booting, sshd down, hung) | Hold current speed for 60 s, then fail safe to 40%. Never lowers the fans while blind |
| CPU at 80°C or above | 60% emergency, bypasses the cap and smoothing |
| Service stopped | Handed back to iDRAC automatic control (systemd `ExecStopPost`) |

Fan speeds are a percentage of max. On an R730, 60% is roughly as loud as POST.

### GPU curve

- Under 45°C: 15%
- 45–65°C: +1% per °C, reaching 35% at the 65°C knee
- Above 65°C: +3% per °C, capped at 50% (reached at 70°C)

Under load the card settles near the knee. The steep section above it acts as a ceiling.

### CPU curve

- Under 55°C: 15% (no influence)
- 55–75°C: linear from 15% to 50%
- 80°C: emergency 60%

For reference, my iDRAC's CPU warning threshold is 87°C and critical is 92°C. iDRAC reads the CPU die temperature, and its critical threshold is the CPU's DTS max. Check yours with `ipmitool sensor | grep -E '^Temp'`.

### Smoothing

Fans go up immediately and come down slowly: only once they're 3% or more above the target, and only 2% per check. Stepped curves make fans "hunt," bouncing between two speeds as the temperature crosses a bracket line. This stops that, and after a gaming session the fans taper off over a few minutes instead of dropping all at once.

### Why it never hands control back to iDRAC mid-load

An earlier version gave the fans back to iDRAC if the CPU got hot. That was a bad idea: iDRAC's automatic mode doesn't know the GPU exists, and it's comfortable running low fan speeds with a hot CPU. Handing back mid-game could lower the fans at exactly the moment the GPU needs them. The script keeps control and takes the higher of both curves instead.

## Results (my R730)

- **Before:** first launch of Cyberpunk 2077, 86°C within minutes, server fans at idle.
- **PassMark GPU test** (early version): 61°C peak, fans topped out at 30%.
- **Diablo IV, max settings, DLSS, no RT:** in the 70s on an earlier stepped curve; **66°C peak at 38% fans** on this version.
- **Idle with the VM running:** ~40°C at 15–17% fans.
- GPU power limited to 130 W (stock 170 W) with the included Windows script.

**vfio-pci "VM off" measurements**, since people argue about this a lot:

- Whole-server power: 84 W with the VM running idle, 70 W with the VM stopped (the drop took a few minutes after shutdown).
- GPU temperature after ~14 minutes stopped, with iDRAC's idle fans: 48°C (read on the next VM boot).

My card parks fine under vfio-pci. Yours may not, and the fixed 15% while the VM is off gives it airflow either way.

## Tested on

- Dell PowerEdge R730, iDRAC8
- Single Xeon E5-2640 v4 (second socket empty)
- Gigabyte RTX 3060 Gaming OC 12GB, passed through to a Windows 11 Pro VM
- Proxmox VE (Debian trixie base)
- Streaming headless with Apollo + Moonlight

## Compatibility and limitations

- **iDRAC7 / iDRAC8 (12th / 13th gen PowerEdge):** the raw fan commands work. **iDRAC9** only accepts them up to firmware 3.30.30.30. Newer iDRAC9 firmware rejects them, and this script can't control your fans.
- One NVIDIA GPU in one VM. Written for a Windows VM; a Linux VM should work as-is if it has `nvidia-smi` and sshd with key auth, but that's untested.
- **GPU core temperature only.** GDDR6 cards like the 3060 don't expose memory temperature, and memory runs hotter than the core. That's why the knee is conservative.
- CPU temperature parsing expects Dell's sensor naming (`Temp` rows in `ipmitool sdr type temperature`). Dual-socket should work (it takes the hottest CPU), but I've only tested single-socket.
- Manual fan mode doesn't survive a reboot. iDRAC runs the fans during POST and boot until the service starts.
- The defaults are tuned for a 3060 at 130 W in an R730. Tune for your hardware.

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
powershell -ExecutionPolicy Bypass -File .\setup-vm.ps1 -PublicKey "ssh-ed25519 AAAA...your key... root@pve" -PowerLimit 130
```

What it does:

- Installs and starts OpenSSH Server (the first install can take a few minutes) and opens port 22 if needed.
- Installs the host's key. Administrator accounts must use `C:\ProgramData\ssh\administrators_authorized_keys` with locked-down permissions, which is the most common Windows OpenSSH gotcha. The script handles it.
- `-PowerLimit` is optional. It sets the GPU power limit now and at every startup via a Scheduled Task. Check your card's allowed range first with `nvidia-smi -q -d POWER` (Min/Max Power Limit), or leave it out to skip.
- Prints the VM's username and IP for the next step.

### 3. Back on the host: test

```bash
ssh -i /root/.ssh/gpu_fan_key -o StrictHostKeyChecking=no USER@VM-IP "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"
```

A single number with no password prompt means it's working. A password prompt means the key didn't install; go back to step 2.

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

You should see the startup line, then `VM off, CPU=..C -> Fans=15%` or a `GPU=` line. The script only logs when the fan speed changes, so silence is normal.

## Tuning

All settings are at the top of `gpu-fan-curve.sh`. Restart after editing: `systemctl restart gpu-fan-curve.service`

| Setting | Default | What it does |
|---|---|---|
| `INTERVAL` | 15 | Seconds between checks |
| `KNEE` | 65 | GPU temperature where the curve turns steep; fans hit `MAX_FAN` at `KNEE`+5. Lower = cooler and louder |
| `MAX_FAN` | 50 | Normal fan cap (%) |
| `DEADBAND` | 3 | Fans only come down once they're this far above target |
| `STEP_DOWN` | 2 | % dropped per check when coming down |
| `OFF_FAN` | 15 | Fan % while the VM is stopped |
| `BLIND_FAN` | 40 | Fail-safe % when the VM is running but the GPU can't be read |
| `BLIND_GRACE` | 60 | Seconds to hold before failing safe (covers VM boot) |
| `CPU_START` / `CPU_FULL` | 55 / 75 | CPU temperatures where the CPU curve starts / reaches `MAX_FAN` |
| `CPU_EMERG` | 80 | CPU temperature for the emergency speed. Keep it below your iDRAC's CPU warning threshold |
| `EMERG_FAN` | 60 | Emergency fan % |

## Useful commands

```bash
journalctl -u gpu-fan-curve.service -f    # live log
journalctl -u gpu-fan-curve.service -b    # this boot only
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

In the Windows VM, remove the power limit task with:

```powershell
Unregister-ScheduledTask -TaskName "GPU Power Limit" -Confirm:$false
```

## Safety

No warranty. This controls your server's cooling. What it does to fail safe:

- If the service stops for any reason, systemd hands the fans back to iDRAC automatic control.
- If the VM is running but the GPU can't be read, it holds the current speed, then goes to 40%. It never lowers the fans while blind.
- If Proxmox can't report the VM's state, that's treated the same as blind.
- The CPU emergency tier kicks in at 80°C, regardless of anything else.
- Config errors make the script exit before it touches the fans. iDRAC stays in automatic mode, and the journal tells you what's wrong.

Behind all of that, both the CPU and GPU throttle and ultimately shut themselves down at their own limits regardless of fan speed. Watch the journal for the first few days anyway.

## About this project

I'm not a coder. I built this over about a week with Claude (Anthropic's AI) writing most of the code. My part was the hardware, the testing, the measurements, and pushing back whenever an answer didn't match what the server actually did, which happened more than once. Everything described here was tested on my R730. Read the script before you run it: it's short, commented, and it controls your server's cooling.

## License

MIT. See [LICENSE](LICENSE).
