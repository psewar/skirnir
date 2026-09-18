# ollama-router-agent: meldet GPU-Fakten dieses Rechners an den Router auf router-host.
# Keine Entscheidungen hier - nur Messwerte. Policy liegt im Router (config.yaml).
# Läuft als Dauerschleife (Scheduled Task AtLogOn, siehe Install-Task.ps1).
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'agent-config.json'),
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
$logPath = Join-Path $PSScriptRoot 'agent.log'
$url = "$($cfg.router)/v1/heartbeat/$($cfg.node)"
$headers = @{ 'X-Router-Token' = $cfg.token }

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 1MB)) { Clear-Content $logPath }
    Add-Content -Path $logPath -Value $line -Encoding UTF8
}

# Einzelinstanz-Schutz (Task-Neustart darf keinen zweiten Sender hinterlassen)
$mutex = New-Object System.Threading.Mutex($false, 'Global\OllamaRouterAgent')
if (-not $mutex.WaitOne(0)) { Log 'already running, exit'; exit 0 }

$smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if (-not $smi) { Log 'nvidia-smi.exe nicht gefunden'; exit 1 }

Log "start node=$($cfg.node) router=$($cfg.router) interval=$($cfg.interval_s)s"
$lastState = ''
$failures = 0
while ($true) {
    try {
        $raw = & $smi.Source --query-gpu=utilization.gpu,memory.total,memory.used,memory.free --format=csv,noheader,nounits 2>$null
        $first = ($raw | Select-Object -First 1) -split ',\s*'
        $body = @{
            gpu_util_pct   = [int]$first[0]
            vram_total_mib = [int]$first[1]
            vram_used_mib  = [int]$first[2]
            vram_free_mib  = [int]$first[3]
            ts             = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        } | ConvertTo-Json -Compress
        $resp = Invoke-RestMethod -Method Post -Uri $url -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 5
        if ($resp.state -ne $lastState) { Log "router sees us as '$($resp.state)' $($resp.busy_reason)"; $lastState = $resp.state }
        $failures = 0
    } catch {
        $failures++
        if ($failures -eq 1 -or ($failures % 100) -eq 0) { Log "heartbeat failed ($failures): $($_.Exception.Message)" }
        $lastState = ''
    }
    if ($Once) { break }
    Start-Sleep -Seconds ([int]$cfg.interval_s)
}
