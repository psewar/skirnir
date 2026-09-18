# Umschalten auf den Dienst (einmal als Administrator):
#   1. Binary nach C:\Program Files\ollama-router-agent kopieren
#   2. alte Dauerlauf-Tasks stoppen und entfernen (OllamaRouterAgent; weitere standortspezifische per -LegacyTasks)
#   3. Dienst anlegen (Konto, ACLs, Firewall, Steuerrecht) und starten
#   4. Zustand zeigen
# Voraussetzung: C:\ProgramData\ollama-router-agent\config.yaml liegt schon da (Vorlage: config.example.yaml).
# Rueckbau: ollama-router-agent.exe uninstall; dann agent\Install-Task.ps1 (und ggf. die eigenen Installer der entfernten Tasks).
param([switch]$RemoveOpenOllamaRules,   # entfernt die Installer-Regeln 'ollama.exe' (Quelle: Any) - Ollama nur noch via TLS-Proxy/Router
      [switch]$RemoveOllamaLanRules,    # entfernt 'Ollama 11434 - ha-host/nodered-host': seit dem Tunnel gibt es keinen Direktzugriff mehr (2026-09-09)
      [string[]]$LegacyTasks = @('OllamaRouterAgent'))   # alte Dauerlauf-Tasks, die der Dienst ersetzt (standortspezifische Namen hier anhaengen)
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')) {
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
$src    = Join-Path $PSScriptRoot 'dist\ollama-router-agent.exe'
$dstDir = 'C:\Program Files\ollama-router-agent'
$exe    = Join-Path $dstDir 'ollama-router-agent.exe'
$cfg    = 'C:\ProgramData\ollama-router-agent\config.yaml'
if (-not (Test-Path $cfg)) { Write-Host "Config fehlt: $cfg" -ForegroundColor Red; exit 1 }

Write-Host "[1/4] Binary" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
if (Get-Service OllamaRouterAgent -ErrorAction SilentlyContinue) {
    Write-Host '      Dienst existiert schon -> stoppen, Binary ersetzen, starten (Update-Pfad)'
    Stop-Service OllamaRouterAgent -ErrorAction SilentlyContinue   # nicht ueber die alte Binary stoppen: die kennt die neue Config evtl. nicht
    (Get-Service OllamaRouterAgent).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    Start-Sleep -Seconds 1
    Copy-Item $src $exe -Force
    & $exe apply-rules --config $cfg
    & $exe start --config $cfg
    Start-Sleep -Seconds 6
    & $exe status --config $cfg
    if ($RemoveOpenOllamaRules) { Remove-OpenOllamaRules }
    if ($RemoveOllamaLanRules) { Remove-OllamaLanRules }
    exit 0
}
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
    $_.CommandLine -like '*ollama-router-agent.ps1*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "      Prozess $($_.ProcessId) ($($_.Name)) beendet" }
Start-Sleep -Seconds 2

Write-Host "[3/4] Dienst anlegen und starten" -ForegroundColor Cyan
& $exe install --config $cfg
if ($LASTEXITCODE -ne 0) { Write-Host 'install fehlgeschlagen' -ForegroundColor Red; exit 1 }
& $exe start --config $cfg
Start-Sleep -Seconds 8

Write-Host "[4/4] Zustand" -ForegroundColor Cyan
& $exe status --config $cfg
if ($RemoveOpenOllamaRules) { Remove-OpenOllamaRules }
if ($RemoveOllamaLanRules) { Remove-OllamaLanRules }
Write-Host "`nFertig. Logs: C:\ProgramData\ollama-router-agent\logs\  (agent.log, stt.log)" -ForegroundColor Green
Start-Sleep -Seconds 3
