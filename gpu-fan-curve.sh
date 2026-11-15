#!/bin/bash
# iDRAC GPU Fan Control
# Drives Dell PowerEdge fans (iDRAC7/iDRAC8) from a passed-through GPU's temperature
# (read over SSH from inside the VM) and the CPU temperature (read from iDRAC).
# Symmetric 4-point curves: GPU 10% -> 50% @ 70C -> 100% @ 83C,
# CPU 10% -> 50% @ 75C -> 100% @ 87C. Proportional ramp-down, VM-aware,
# periodic/delta temp logging, automatic iDRAC restore on exit.
# See README.md for install and tuning. MIT License.

# ---- Required: set these three ----
VMID="CHANGE_ME"                 # Proxmox VM ID of the GPU VM (run: qm list)
VM_IP="CHANGE_ME"                # the VM's IP address
VM_USER="CHANGE_ME"              # Windows account the host SSHes in as
SSH_KEY="/root/.ssh/gpu_fan_key"
INTERVAL=15          # seconds between checks
STATUS_INTERVAL=300  # seconds between heartbeat temp logs when fans are steady (5 min)
TEMP_LOG_DELTA=5     # also log if GPU or CPU moves >= this many C since last log

# Shared fan duty-cycle anchors
MIN_FAN=10           # lowest active fan % (safe above R730 iDRAC low-RPM warning threshold)
MAX_FAN=50           # normal acoustic cap at GPU_GOAL / CPU_GOAL (60%+ = jet engine)
ABS_MAX_FAN=100      # full 100% ceiling at GPU_MAX_TEMP / CPU_MAX_TEMP

# GPU thermal curve anchors
GPU_MIN_TEMP=35      # at or below this -> MIN_FAN
KNEE=65              # start of steeper ramp toward GPU_GOAL (35%)
GPU_GOAL=70          # target load temp -> fans reach MAX_FAN (50%)
GPU_MAX_TEMP=83      # RTX 3060 thermal limit -> fans reach ABS_MAX_FAN (100%)

# CPU thermal curve anchors
CPU_MIN_TEMP=35      # at or below this -> MIN_FAN
CPU_START=55         # CPU temp where ramp accelerates above 15%
CPU_GOAL=75          # target load temp -> fans reach MAX_FAN (50%)
CPU_MAX_TEMP=87      # iDRAC CPU warning threshold -> fans reach ABS_MAX_FAN (100%)

DEADBAND=3           # fans only step down once at least this far above target
STEP_DOWN=2          # minimum % drop per check when settling near target
OFF_FAN=10           # VM stopped: card parked under vfio
BLIND_FAN=25         # VM running but GPU/CPU temp unreadable past the grace period
BLIND_GRACE=60       # seconds to hold current speed before failing high (covers boot)

log() { echo "$(date): $*"; }

for cmd in ipmitool qm ssh; do
    command -v "$cmd" >/dev/null 2>&1 || { log "FATAL: missing required command '$cmd'"; exit 1; }
done

CLEANED_UP=0
cleanup() {
    (( CLEANED_UP )) && return
    CLEANED_UP=1
    log "Restoring iDRAC automatic fan control"
    ipmitool raw 0x30 0x30 0x01 0x01 >/dev/null 2>&1 || true
    exit 0
}
trap cleanup EXIT INT TERM

gpu_curve() {
    local t=$1 s
    if   (( t <= GPU_MIN_TEMP )); then
        s=$MIN_FAN
    elif (( t <= KNEE )); then
        s=$(( MIN_FAN + (t - GPU_MIN_TEMP) * (35 - MIN_FAN) / (KNEE - GPU_MIN_TEMP) ))
    elif (( t <= GPU_GOAL )); then
        s=$(( 35 + (t - KNEE) * (MAX_FAN - 35) / (GPU_GOAL - KNEE) ))
    elif (( t < GPU_MAX_TEMP )); then
        s=$(( MAX_FAN + (t - GPU_GOAL) * (ABS_MAX_FAN - MAX_FAN) / (GPU_MAX_TEMP - GPU_GOAL) ))
    else
        s=$ABS_MAX_FAN
    fi
    echo "$s"
}

cpu_curve() {
    local c=$1 s
    if   (( c <= CPU_MIN_TEMP )); then
        s=$MIN_FAN
    elif (( c < CPU_START )); then
        s=$(( MIN_FAN + (c - CPU_MIN_TEMP) * (15 - MIN_FAN) / (CPU_START - CPU_MIN_TEMP) ))
    elif (( c <= CPU_GOAL )); then
        s=$(( 15 + (c - CPU_START) * (MAX_FAN - 15) / (CPU_GOAL - CPU_START) ))
    elif (( c < CPU_MAX_TEMP )); then
        s=$(( MAX_FAN + (c - CPU_GOAL) * (ABS_MAX_FAN - MAX_FAN) / (CPU_MAX_TEMP - CPU_GOAL) ))
    else
        s=$ABS_MAX_FAN
    fi
    echo "$s"
}

cpu_temp() {
    ipmitool sdr type temperature 2>/dev/null \
        | awk -F'|' '$1 ~ /^Temp/ && $5 ~ /degrees/ {gsub(/[^0-9]/, "", $5); print $5}' \
        | sort -n | tail -1
}

to_hex() { printf "0x%02x" "$1"; }

IPMI_ERR=0
set_fans() {
    if ipmitool raw 0x30 0x30 0x01 0x00 >/dev/null 2>&1 && \
       ipmitool raw 0x30 0x30 0x02 0xff "$(to_hex "$1")" >/dev/null 2>&1; then
        if (( IPMI_ERR )); then
            log "IPMI fan control recovered"
            IPMI_ERR=0
        fi
        return 0
    else
        if (( ! IPMI_ERR )); then
            log "WARNING: ipmitool failed to set fan speed to ${1}%"
            IPMI_ERR=1
        fi
        return 1
    fi
}

CURRENT=-1           # -1 = unknown / iDRAC in control
BLIND=0
CPU_ERR=0
LAST_LOG_SEC=0
LAST_LOG_GPU=""
LAST_LOG_CPU=""

log "Fan control starting — VM ${VMID}, GPU ${GPU_MIN_TEMP}C(${MIN_FAN}%)/${KNEE}C(35%)/${GPU_GOAL}C(${MAX_FAN}%)/${GPU_MAX_TEMP}C(${ABS_MAX_FAN}%), CPU ${CPU_MIN_TEMP}C(${MIN_FAN}%)/${CPU_START}C(15%)/${CPU_GOAL}C(${MAX_FAN}%)/${CPU_MAX_TEMP}C(${ABS_MAX_FAN}%)"

while true; do
    NOW=$SECONDS
    GPU_TEMP=""

    RAW_CPU=$(cpu_temp)
    CPU_VALID=0
    if [[ "$RAW_CPU" =~ ^[0-9]+$ ]]; then
        CPU=$RAW_CPU
        CPU_VALID=1
        if (( CPU_ERR )); then
            log "CPU temperature sensor recovered (${CPU}C)"
            CPU_ERR=0
        fi
    else
        CPU=0
        if (( ! CPU_ERR )); then
            log "WARNING: CPU temperature unreadable from iDRAC — holding safe floor"
            CPU_ERR=1
        fi
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
            RAW_GPU=$(ssh -i "$SSH_KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
                -o BatchMode=yes "$VM_USER@$VM_IP" \
                "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader" 2>/dev/null | tr -d '[:space:]')
            RAW_GPU=${RAW_GPU%%.*}   # driver 620/621 emits floats (43.19) -> 43
            if [[ "$RAW_GPU" =~ ^[0-9]+$ ]]; then
                BLIND=0
                GPU_TEMP=$RAW_GPU
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

    # CPU gets a vote: fans follow whichever part wants more (or hold safe floor if CPU sensor drops)
    if (( CPU_VALID )); then
        CPU_WANT=$(cpu_curve "$CPU")
        CPU_LABEL="CPU=${CPU}C"
    else
        CPU_WANT=$BLIND_FAN
        (( CURRENT > CPU_WANT )) && CPU_WANT=$CURRENT
        CPU_LABEL="CPU=unreadable"
    fi

    if [[ -z "$WANT" ]]; then
        # Holding through the blind grace period — CPU still sets initial speed or pushes fans up
        if (( CURRENT == -1 || CPU_WANT > CURRENT )); then
            WANT=$CPU_WANT
            REASON="GPU unreadable (holding), ${CPU_LABEL} driving"
        else
            REASON="GPU unreadable (holding), ${CPU_LABEL}"
        fi
    elif (( CPU_WANT > WANT )); then
        WANT=$CPU_WANT
        REASON="${REASON}, ${CPU_LABEL} driving"
    else
        REASON="${REASON}, ${CPU_LABEL}"
    fi

    FAN_CHANGED=0
    if [[ -n "$WANT" ]]; then
        if (( CURRENT == -1 || WANT > CURRENT )); then
            NEW=$WANT
        elif (( CURRENT - WANT >= DEADBAND || ((WANT <= MIN_FAN || WANT <= OFF_FAN) && CURRENT > WANT) )); then
            GAP=$(( CURRENT - WANT ))
            DROP=$(( GAP / 3 ))
            (( DROP < STEP_DOWN )) && DROP=$STEP_DOWN
            NEW=$(( CURRENT - DROP ))
            (( NEW < WANT )) && NEW=$WANT
        else
            NEW=$CURRENT
        fi

        if (( NEW != CURRENT )); then
            if set_fans "$NEW"; then
                CURRENT=$NEW
                FAN_CHANGED=1
            fi
        fi
    fi

    # Check if either sensor shifted >= TEMP_LOG_DELTA since last log
    TEMP_SHIFT=0
    if [[ -n "$GPU_TEMP" && -n "$LAST_LOG_GPU" ]]; then
        (( GPU_TEMP - LAST_LOG_GPU >= TEMP_LOG_DELTA || LAST_LOG_GPU - GPU_TEMP >= TEMP_LOG_DELTA )) && TEMP_SHIFT=1
    fi
    if (( CPU_VALID )) && [[ -n "$LAST_LOG_CPU" ]]; then
        (( CPU - LAST_LOG_CPU >= TEMP_LOG_DELTA || LAST_LOG_CPU - CPU >= TEMP_LOG_DELTA )) && TEMP_SHIFT=1
    fi

    if (( FAN_CHANGED )); then
        log "${REASON} -> Fans=${CURRENT}%"
        LAST_LOG_SEC=$NOW
        LAST_LOG_GPU=$GPU_TEMP
        (( CPU_VALID )) && LAST_LOG_CPU=$CPU || LAST_LOG_CPU=""
    elif (( CURRENT >= 0 && NOW - LAST_LOG_SEC >= STATUS_INTERVAL )); then
        log "[status] ${REASON} -> Fans=${CURRENT}%"
        LAST_LOG_SEC=$NOW
        LAST_LOG_GPU=$GPU_TEMP
        (( CPU_VALID )) && LAST_LOG_CPU=$CPU || LAST_LOG_CPU=""
    elif (( CURRENT >= 0 && TEMP_SHIFT )); then
        log "[temp] ${REASON} -> Fans=${CURRENT}%"
        LAST_LOG_SEC=$NOW
        LAST_LOG_GPU=$GPU_TEMP
        (( CPU_VALID )) && LAST_LOG_CPU=$CPU || LAST_LOG_CPU=""
    fi

    sleep "$INTERVAL" &
    wait $!
done
