<#
Trusted update keys of the agent on THIS node (ADR-0058, ADR-0066). Run as administrator; Windows PowerShell 5.1 or 7.
Set-UpdateKey.cmd next to this file starts it with -ExecutionPolicy Bypass for this one call.

  -Step Add      -Key <new public key>   rotation step A: add the key to update.public_keys; update.public_key stays valid
  -Step Complete -Key <new public key>   rotation step C: the key becomes update.public_key, public_keys is removed
                                          (the old key is no longer trusted)
  -Step Replace  -Key <new public key>   the signing key is LOST: the key becomes update.public_key, public_keys is
                                          removed; the node trusts only the new key from now on
  -Step Check                            show the trusted keys (shortened) and whether the agent knows public_keys

Checks before acting: administrator rights, service and configuration path, agent >= 0.29.0 (older agents ignore
public_keys and would lock themselves out), key format (base64, 32 bytes), an update block. Then: backup next to the file,
change, `check-config` with the installed binary, service restart. If a step fails the backup is restored and the service
restarted. The last line is always STATE: changed | current | checked | refused | restored.
#>
param(
    [Parameter(Mandatory)] [ValidateSet('Add', 'Complete', 'Replace', 'Check')] [string] $Step,
    [string] $Key = ''
)
$ErrorActionPreference = 'Stop'

function Finish([string] $state, [string] $text, [int] $code) {
    Write-Host $text
    Write-Host "STATE: $state"
    exit $code
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Finish 'refused' 'Start PowerShell with "Run as administrator".' 4 }

$service = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq 'SkirnirAgent' }
if (-not $service) { Finish 'refused' 'Service SkirnirAgent not found.' 4 }
if ($service.PathName -notmatch '^"?(?<exe>[^"]+\.exe)"?\s+--config\s+"?(?<cfg>[^"]+?)"?\s*$') { Finish 'refused' "Service command line not recognised: $($service.PathName)" 4 }
$exe, $cfg = $Matches.exe, $Matches.cfg
if (-not (Test-Path $exe) -or -not (Test-Path $cfg)) { Finish 'refused' "Binary or configuration missing: $exe / $cfg" 4 }

# Native programs write to stderr; Windows PowerShell 5.1 turns that into a terminating error under 'Stop'.
$ErrorActionPreference = 'Continue'
$version = ("$(& $exe version 2>$null | Select-Object -First 1)").Trim()
$ErrorActionPreference = 'Stop'
$parts = ($version -replace '^[^0-9]*', '') -split '\.'
$recent = $parts.Count -ge 2 -and (([int]$parts[0] -gt 0) -or ([int]$parts[1] -ge 29))
Write-Host "Agent: $version  Configuration: $cfg"

# 5.1 and 7 read and write differently (5.1: ANSI without BOM, no utf8NoBOM) - use .NET directly.
$utf8 = New-Object System.Text.UTF8Encoding $false
$text = [System.IO.File]::ReadAllText($cfg, $utf8)
$hasUpdate = $text -match '(?m)^update:\s*$'
$keyLines = ([regex]::Matches($text, '(?m)^\s+public_keys?:.*$') | ForEach-Object { ($_.Value.Trim() -replace '(:\s*\[?)\s*([A-Za-z0-9+/=]{8})[A-Za-z0-9+/=]*', '$1$2...') }) -join '; '
if ($Step -eq 'Check') {
    Finish 'checked' "update block: $hasUpdate; keys: $keyLines; agent >= 0.29.0: $recent" 0
}

if (-not $recent) { Finish 'refused' "Agent $version does not know update.public_keys yet. Update to 0.29.0 first (router rollout)." 3 }
if (-not $hasUpdate) { Finish 'refused' 'No update: block in the configuration - without an operator key there is nothing to change.' 4 }
$raw = @()
if ($Key.Trim() -match '^[A-Za-z0-9+/]{43}=$') { $raw = [Convert]::FromBase64String($Key.Trim()) }   # format checked first
if ($raw.Length -ne 32) { Finish 'refused' 'The key is not an Ed25519 public key (base64, 32 bytes).' 4 }
$k = $Key.Trim()

$new = $text
if ($Step -eq 'Add') {
    if ($text -match [regex]::Escape($k)) { Finish 'current' 'The key is already listed.' 0 }
    if ($new -match '(?m)^(\s+)public_keys:\s*\[(.*)\]\s*$') {
        $new = [regex]::Replace($new, '(?m)^(\s+)public_keys:\s*\[(.*)\]\s*$', { param($m) "$($m.Groups[1].Value)public_keys: [$($m.Groups[2].Value.Trim().TrimEnd(',')), $k]" -replace '\[, ', '[' })
    } else {
        $new = [regex]::Replace($new, '(?m)^update:\s*$', "update:`r`n  public_keys: [$k]   # ADR-0058: next signing key (rotation $(Get-Date -Format yyyy-MM-dd))", 1)
    }
} else {
    if ($Step -eq 'Complete' -and $new -notmatch [regex]::Escape($k)) { Finish 'refused' 'The new key is not in the configuration - run -Step Add first.' 4 }
    $new = [regex]::Replace($new, '(?m)^\s+public_keys:.*\r?\n', '')
    if ($new -match '(?m)^(\s+)public_key:\s*\S+') {
        $new = [regex]::Replace($new, '(?m)^(\s+)public_key:\s*\S+', { param($m) "$($m.Groups[1].Value)public_key: $k" }, 1)
    } else {
        $new = [regex]::Replace($new, '(?m)^update:\s*$', "update:`r`n  public_key: $k", 1)
    }
}
if ($new -eq $text) { Finish 'current' 'Nothing to change.' 0 }

$backup = "$cfg.before-key-change-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item -Path $cfg -Destination $backup
try {
    [System.IO.File]::WriteAllText($cfg, $new, $utf8)
    $ErrorActionPreference = 'Continue'
    $checked = (& $exe check-config --config $cfg 2>&1 | ForEach-Object { "$_" }) -join ' '
    $checkCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($checkCode -ne 0) { throw "check-config: $checked" }
    Restart-Service -Name SkirnirAgent
    Start-Sleep -Seconds 5
    if ((Get-Service SkirnirAgent).Status -ne 'Running') { throw 'The service does not run after the restart.' }
} catch {
    # exception-ok: file system and service control; every failure takes the same way back
    Copy-Item -Path $backup -Destination $cfg -Force
    Restart-Service -Name SkirnirAgent -ErrorAction SilentlyContinue
    Finish 'restored' "Failed: $($_.Exception.Message). Backup restored: $backup" 1
}
Finish 'changed' "Step $Step done. Backup: $backup. check-config: $checked" 0
