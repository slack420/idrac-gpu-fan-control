#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Windows VM side of iDRAC GPU Fan Control.

.DESCRIPTION
    - Installs and starts OpenSSH Server, set to start automatically
    - Makes sure the firewall allows SSH (port 22)
    - Installs the Proxmox host's public key for passwordless SSH
      (both the administrator key file and your own profile, so it works for either account type)
    - Optionally sets a GPU power limit now and at every startup (Scheduled Task, runs as SYSTEM)

    Run it as the Windows account the Proxmox host will SSH in as.

.PARAMETER PublicKey
    The host's public key: the full line from /root/.ssh/gpu_fan_key.pub (starts with ssh-ed25519).

.PARAMETER PowerLimit
    Optional. GPU power limit in watts. Check your card's allowed range first: nvidia-smi -q -d POWER

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-vm.ps1 -PublicKey "ssh-ed25519 AAAA... root@pve" -PowerLimit 130
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$PublicKey,

    [int]$PowerLimit = 0
)

$ErrorActionPreference = 'Stop'
$PublicKey = $PublicKey.Trim()

if ($PublicKey -notmatch '^ssh-(ed25519|rsa) ') {
    throw "That doesn't look like a public key. Paste the whole line from /root/.ssh/gpu_fan_key.pub (starts with ssh-ed25519)."
}

# --- NVIDIA driver ---
$smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if (-not $smi) {
    throw "nvidia-smi.exe not found. Install the NVIDIA driver in this VM first."
}
Write-Host "[ok] nvidia-smi found: $($smi.Source)"

# --- OpenSSH Server ---
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if ($cap.State -ne 'Installed') {
    Write-Host '[..] Installing OpenSSH Server (can take a few minutes)...'
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Write-Host '[ok] OpenSSH Server running, starts automatically'

if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    Write-Host '[ok] Firewall rule added for port 22'
} else {
    Write-Host '[ok] Firewall rule for port 22 already present'
}

# --- Host key ---
function Add-KeyLine([string]$Path, [string]$Key) {
    $dir = Split-Path $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $existing = if (Test-Path $Path) { Get-Content $Path } else { @() }
    if ($existing -notcontains $Key) {
        Add-Content -Path $Path -Value $Key -Encoding Ascii
    }
}

# Administrator accounts: Windows OpenSSH ignores your profile and only reads this file,
# and only if its permissions are locked to Administrators + SYSTEM (SIDs, so it works on any language).
$adminKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
Add-KeyLine $adminKeys $PublicKey
icacls.exe $adminKeys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $adminKeys" }
Write-Host "[ok] Key installed for administrator accounts: $adminKeys"

# Standard accounts: your own profile
$userKeys = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
Add-KeyLine $userKeys $PublicKey
Write-Host "[ok] Key installed for standard accounts: $userKeys"

# --- GPU power limit (optional) ---
if ($PowerLimit -gt 0) {
    & $smi.Source -pl $PowerLimit
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi rejected $PowerLimit W. Check Min/Max Power Limit with: nvidia-smi -q -d POWER"
    }
    $action    = New-ScheduledTaskAction -Execute $smi.Source -Argument "-pl $PowerLimit"
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName 'GPU Power Limit' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "Set GPU power limit to $PowerLimit W at startup" -Force | Out-Null
    Write-Host "[ok] GPU power limit set to $PowerLimit W now and at every startup (task: GPU Power Limit)"
} else {
    Write-Host '[--] No -PowerLimit given, skipped'
}

# --- Values for the Proxmox side ---
$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress

Write-Host ''
Write-Host 'Done. Put these in gpu-fan-curve.sh on the Proxmox host:'
Write-Host "  VM_USER=`"$env:USERNAME`""
foreach ($ip in $ips) { Write-Host "  VM_IP=`"$ip`"" }
