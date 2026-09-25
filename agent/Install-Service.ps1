# Fuer Menschen gibt es seit 0.10.0 `skirnir-agent.exe setup` (Doppelklick, UAC, Fragen) - dieses Skript bleibt fuer
# Sonderfaelle (alte Tasks entfernen, Firewall-Regeln des Ollama-Installers).
# Umschalten auf den Dienst (einmal als Administrator):
#   1. Binary nach C:\Program Files\skirnir-agent kopieren
#   2. alte Dauerlauf-Tasks stoppen und entfernen (SkirnirAgent; weitere standortspezifische per -LegacyTasks)
#   3. Dienst anlegen (Konto, ACLs, Firewall, Steuerrecht) und starten
#   4. Zustand zeigen
# Voraussetzung: C:\ProgramData\skirnir-agent\config.yaml liegt schon da (Vorlage: config.example.yaml).
# Rueckbau: skirnir-agent.exe uninstall (und ggf. die eigenen Installer der entfernten Tasks).
param([switch]$RemoveOpenOllamaRules,   # entfernt die Installer-Regeln 'ollama.exe' (Quelle: Any) - Ollama nur noch via TLS-Proxy/Router
      [switch]$RemoveOllamaLanRules,    # entfernt 'Ollama 11434 - ha-host/nodered-host': seit dem Tunnel gibt es keinen Direktzugriff mehr (2026-09-09)
      [switch]$SkipGpuzRelay,           # keine Aufgabe fuer das GPU-Z-Relay anlegen (bzw. eine vorhandene entfernen)
      [string[]]$LegacyTasks = @('SkirnirAgent'))   # alte Dauerlauf-Tasks, die der Dienst ersetzt (standortspezifische Namen hier anhaengen)
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Bitte als Administrator ausfuehren.' -ForegroundColor Red; exit 1
}
function Remove-OllamaLanRules {
    # 'Ollama 11434 - ha-host' / '- nodered-host' stammen aus der Zeit vor dem Tunnel (Direktzugriff von HA/Node-RED auf
    # Ollama). Seit 2026-09-07 laeuft alles ueber den Router-Tunnel, seit 2026-09-09 mit Client-Auth - kein Direktzugriff mehr.
    $r = Get-NetFirewallRule -DisplayName 'Ollama 11434 - *' -ErrorAction SilentlyContinue
    if ($r) { $r | ForEach-Object { Write-Host "      entferne: $($_.DisplayName)" }; $r | Remove-NetFirewallRule }
    else { Write-Host "      keine 'Ollama 11434 - *'-Regeln mehr vorhanden" }
}

function Remove-OpenOllamaRules {
    # Ollama-Installer legt 'ollama.exe'-Regeln fuer Quelle Any an -> die GPU war fuer das ganze LAN offen (2026-09-07 gefunden).
    $r = Get-NetFirewallRule -DisplayName 'ollama.exe' -ErrorAction SilentlyContinue
    if ($r) { $r | Remove-NetFirewallRule; Write-Host "      $(@($r).Count) offene Ollama-Firewallregel(n) 'ollama.exe' entfernt" }
    Get-NetFirewallRule -DisplayName 'Ollama 11434 - *' | ForEach-Object { Write-Host "      bleibt: $($_.DisplayName)" }
}
function Stop-GpuzRelay {
    # Das Relay laeuft aus derselben Binary (Aufgabe in der Anmeldesitzung) und haelt sie offen - vor dem Ersetzen beenden.
    $tn = 'SkirnirAgent-GpuzRelay'
    if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name = 'skirnir-agent.exe'" | Where-Object { $_.CommandLine -like '*gpuz-relay*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "      Relay-Prozess $($_.ProcessId) beendet" }
    Start-Sleep -Seconds 1
}

function Install-GpuzRelayTask {
    # GPU-Z-Sensoren (0.6.0): GPU-Z legt sein Shared-Memory-Objekt in der Anmeldesitzung an, mit einer DACL fuer SYSTEM,
    # Administratoren und die eigene Sitzung. Das virtuelle Dienstkonto darf es nicht lesen -> eine Aufgabe "bei Anmeldung"
    # (Gruppe Benutzer, ohne Adminrechte, versteckt, conhost --headless gegen ein Terminalfenster) reicht die Werte per
    # localhost an den Dienst. Ohne GPU-Z wartet das Relay still. Abschalten: Aufgabe entfernen oder gpuz.enabled: false.
    $tn = 'SkirnirAgent-GpuzRelay'
    if ($SkipGpuzRelay) {
        if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $tn -Confirm:$false
            Write-Host "      Aufgabe $tn entfernt (-SkipGpuzRelay)"
        }
        return
    }
    $action   = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless `"$exe`" gpuz-relay --config `"$cfg`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval ([TimeSpan]::FromMinutes(1)) `
                -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited   # Benutzer, interaktiv, keine Adminrechte
    Register-ScheduledTask -TaskName $tn -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Get-CimInstance Win32_Process -Filter "Name = 'skirnir-agent.exe'" | Where-Object { $_.CommandLine -like '*gpuz-relay*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName $tn
    Write-Host "      Aufgabe $tn (GPU-Z-Relay bei Anmeldung) angelegt und gestartet"
}
$src    = Join-Path $PSScriptRoot 'dist\skirnir-agent.exe'
$dstDir = 'C:\Program Files\skirnir-agent'
$exe    = Join-Path $dstDir 'skirnir-agent.exe'
$cfg    = 'C:\ProgramData\skirnir-agent\config.yaml'
if (-not (Test-Path $cfg)) { Write-Host "Config fehlt: $cfg" -ForegroundColor Red; exit 1 }

Write-Host "[1/4] Binary" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
if (Get-Service SkirnirAgent -ErrorAction SilentlyContinue) {
    Write-Host '      Dienst existiert schon -> stoppen, Binary ersetzen, starten (Update-Pfad)'
    Stop-Service SkirnirAgent -ErrorAction SilentlyContinue   # nicht ueber die alte Binary stoppen: die kennt die neue Config evtl. nicht
    (Get-Service SkirnirAgent).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    Stop-GpuzRelay
    Copy-Item $src $exe -Force
    & $exe apply-rules --config $cfg
    & $exe start --config $cfg
    Start-Sleep -Seconds 6
    & $exe status --config $cfg
    Install-GpuzRelayTask
    if ($RemoveOpenOllamaRules) { Remove-OpenOllamaRules }
    if ($RemoveOllamaLanRules) { Remove-OllamaLanRules }
    exit 0
}
Stop-GpuzRelay
Copy-Item $src $exe -Force
& $exe version

Write-Host "[2/4] Alte Tasks" -ForegroundColor Cyan
foreach ($t in $LegacyTasks) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "      Task $t entfernt"
    }
}
# Prozesse der alten Tasks beenden (halten Port 10300 und die MQTT-Client-ID)
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*skirnir-agent.ps1*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "      Prozess $($_.ProcessId) ($($_.Name)) beendet" }
Start-Sleep -Seconds 2

Write-Host "[3/4] Dienst anlegen und starten" -ForegroundColor Cyan
& $exe install --config $cfg
if ($LASTEXITCODE -ne 0) { Write-Host 'install fehlgeschlagen' -ForegroundColor Red; exit 1 }
& $exe start --config $cfg
Start-Sleep -Seconds 8

Write-Host "[4/4] Zustand" -ForegroundColor Cyan
& $exe status --config $cfg
Install-GpuzRelayTask
if ($RemoveOpenOllamaRules) { Remove-OpenOllamaRules }
if ($RemoveOllamaLanRules) { Remove-OllamaLanRules }
Write-Host "`nFertig. Logs: C:\ProgramData\skirnir-agent\logs\  (agent.log, stt.log)" -ForegroundColor Green
Start-Sleep -Seconds 3
