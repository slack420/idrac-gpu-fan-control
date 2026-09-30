#!/bin/bash
# iDRAC GPU Fan Control
# Drives Dell PowerEdge fans (iDRAC7/iDRAC8) from a passed-through GPU's temperature
# (read over SSH from inside the VM) and the CPU temperature (read from iDRAC).
# Fans follow whichever part wants more air. Runs on the Proxmox host as a systemd service.
# See README.md for install and tuning. MIT License.

# ---- Required: set these three ----
VMID="CHANGE_ME"                 # Proxmox VM ID of the GPU VM (run: qm list)
VM_IP="CHANGE_ME"                # the VM's IP address
VM_USER="CHANGE_ME"              # Windows account the host SSHes in as
SSH_KEY="/root/.ssh/gpu_fan_key"

# ---- Tuning (defaults tuned for an RTX 3060 at 130W in an R730) ----
INTERVAL=15      # seconds between checks
KNEE=65          # GPU: where it settles under load; fans hit MAX_FAN at KNEE+5
MAX_FAN=50       # normal loudness cap (60% is roughly POST-loud on an R730)
DEADBAND=3       # fans only come down once they're this far above target
STEP_DOWN=2      # how much fans drop per check when coming down
OFF_FAN=15       # VM stopped: GPU parked under vfio-pci, can't be read
BLIND_FAN=40     # VM running but GPU temp unreadable past the grace period
BLIND_GRACE=60   # seconds to hold current speed before failing high (covers VM boot)
CPU_START=55     # CPU temp where the CPU starts asking for more fan
CPU_FULL=75      # CPU temp where the CPU asks for MAX_FAN
CPU_EMERG=80     # emergency tier; keep below your iDRAC CPU warning threshold
EMERG_FAN=60     # emergency fan speed (loud on purpose)

gpu_curve() {
    local t=$1 s
    if   (( t < KNEE - 20 )); then s=15
    elif (( t <= KNEE ));     then s=$(( 35 + t - KNEE ))
    else                           s=$(( 35 + (t - KNEE) * 3 ))
    fi
    (( s > MAX_FAN )) && s=$MAX_FAN
    echo "$s"
}

cpu_curve() {
    local c=$1
    if   (( c < CPU_START )); then echo 15
    elif (( c < CPU_FULL ));  then echo $(( 15 + (c - CPU_START) * (MAX_FAN - 15) / (CPU_FULL - CPU_START) ))
    else                           echo "$MAX_FAN"
    fi
}

# Hottest CPU from iDRAC. Dell names CPU sensors "Temp"; empty sockets read "Disabled" and are skipped.
cpu_temp() {
    ipmitool sdr type temperature 2>/dev/null \
        | awk -F'|' '$1 ~ /^Temp/ && $5 ~ /degrees/ {gsub(/[^0-9]/, "", $5); print $5}' \
        | sort -n | tail -1
}

to_hex() { printf "0x%02x" "$1"; }

# Dell OEM IPMI: 0x30 0x30 0x01 0x00 = manual mode, 0x30 0x30 0x02 0xff <hex%> = all fans to that %
set_fans() {
    ipmitool raw 0x30 0x30 0x01 0x00 >/dev/null 2>&1
    ipmitool raw 0x30 0x30 0x02 0xff "$(to_hex "$1")" >/dev/null 2>&1
}

log() { echo "$(date): $*"; }

# ---- Startup checks: exit before touching the fans if something's missing ----
for v in VMID VM_IP VM_USER; do
    if [[ "${!v}" == "CHANGE_ME" ]]; then
        log "Config error: set ${v} at the top of $0"
        exit 1
    fi
done
[[ -r "$SSH_KEY" ]] || { log "Config error: SSH key ${SSH_KEY} not found"; exit 1; }
command -v ipmitool >/dev/null 2>&1 || { log "ipmitool not installed (apt install ipmitool)"; exit 1; }
command -v qm >/dev/null 2>&1 || { log "qm not found: this must run on the Proxmox host"; exit 1; }

CURRENT=-1       # -1 = unknown / iDRAC in control
BLIND=0
EMERG=0

log "Fan control starting — VM ${VMID}, GPU knee ${KNEE}C, CPU ${CPU_START}-${CPU_FULL}C, max ${MAX_FAN}%, emergency ${EMERG_FAN}% at CPU ${CPU_EMERG}C"

while true; do
    CPU=$(cpu_temp); CPU=${CPU:-0}

    # Emergency tier: CPU near iDRAC's warning line
    if   (( CPU >= CPU_EMERG ));     then EMERG=1
    elif (( CPU <= CPU_EMERG - 5 )); then EMERG=0
    fi
    if (( EMERG )); then
        if (( CURRENT != EMERG_FAN )); then
            set_fans "$EMERG_FAN"
            CURRENT=$EMERG_FAN
            log "EMERGENCY: CPU=${CPU}C -> Fans=${EMERG_FAN}%"
        fi
        sleep "$INTERVAL"
        continue
    fi

    STATE=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
    WANT=""
    case "$STATE" in
        stopped)
            BLIND=0
            WANT=$OFF_FAN
            REASON="VM off"
            ;;
        running)
            GPU_TEMP=$(ssh -i "$SSH_KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
                -o BatchMode=yes "$VM_USER@$VM_IP" \
                "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader" 2>/dev/null | tr -d '[:space:]')
            if [[ "$GPU_TEMP" =~ ^[0-9]+$ ]]; then
                BLIND=0
                WANT=$(gpu_curve "$GPU_TEMP")
                REASON="GPU=${GPU_TEMP}C"
            fi
            ;;
    esac

    # Running but blind (booting, sshd down, hung) or unknown VM state
    if [[ -z "$WANT" ]]; then
        BLIND=$(( BLIND + 1 ))
        if (( BLIND * INTERVAL >= BLIND_GRACE )); then
            (( BLIND * INTERVAL - INTERVAL < BLIND_GRACE )) && \
                log "GPU temp unreadable ${BLIND_GRACE}s+ (VM state: ${STATE:-unknown}) — failing safe"
            WANT=$BLIND_FAN
            (( CURRENT > WANT )) && WANT=$CURRENT
            REASON="GPU unreadable"
        fi
    fi

    # CPU gets a vote: fans follow whichever part wants more
    CPU_WANT=$(cpu_curve "$CPU")
    if [[ -z "$WANT" ]]; then
        # Holding through the blind grace period — CPU can still push fans up
        if (( CURRENT >= 0 && CPU_WANT > CURRENT )); then
            WANT=$CPU_WANT
            REASON="GPU unreadable (holding), CPU=${CPU}C driving"
        fi
    elif (( CPU_WANT > WANT )); then
        WANT=$CPU_WANT
        REASON="${REASON}, CPU=${CPU}C driving"
    else
        REASON="${REASON}, CPU=${CPU}C"
    fi

    # Smoothing: up immediately, down slowly, ignore small dips
    if [[ -n "$WANT" ]]; then
        if (( CURRENT == -1 || WANT > CURRENT )); then
            NEW=$WANT
        elif (( CURRENT - WANT >= DEADBAND )); then
            NEW=$(( CURRENT - STEP_DOWN ))
            (( NEW < WANT )) && NEW=$WANT
        else
            NEW=$CURRENT
        fi

        if (( NEW != CURRENT )); then
            set_fans "$NEW"
            CURRENT=$NEW
            log "${REASON} -> Fans=${NEW}%"
        fi
    fi

    sleep "$INTERVAL"
done
