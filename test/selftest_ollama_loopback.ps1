# Self-test for agent/Set-OllamaLoopback.ps1 (TM-11, ADR-0072): the pure parts - reading OLLAMA_HOST values, finding the
# Ollama child in an agent configuration and rewriting only its OLLAMA_HOST - and where an elevated run keeps its
# transcript (ADR-0089; on Windows also a real one). Runs with Windows PowerShell 5.1 and pwsh 7
# on any OS:  pwsh -NoProfile -File test/selftest_ollama_loopback.ps1   (exit code 0 = all passed)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'agent' | Join-Path -ChildPath 'Set-OllamaLoopback.ps1')

$Script:Failures = 0
function Assert-Equal($actual, $expected, [string] $what) {
    if ("$actual" -ne "$expected") { Write-Host "FAIL $what`n  expected: $expected`n  actual:   $actual"; $Script:Failures++ }
}
function Assert-True([bool] $value, [string] $what) { if (-not $value) { Write-Host "FAIL $what"; $Script:Failures++ } }
$T = '127.0.0.1:11434'

# values
foreach ($v in @('0.0.0.0', '0.0.0.0:11434', 'http://0.0.0.0:11434', '[::]:11434', '::', ':11434', '192.0.2.5:11434', 'https://node.example:11434/')) {
    Assert-True (-not (Test-LoopbackValue $v)) "not loopback: $v"
}
foreach ($v in @('', $null, '127.0.0.1', '127.0.0.1:11434', 'http://127.0.0.1:11434', '[::1]:11434', '::1', 'localhost:11434', "'127.0.0.1:11434'")) {
    Assert-True (Test-LoopbackValue $v) "loopback: $v"
}
Assert-Equal (Get-HostOfValue '[::]:11434') '::' 'host of [::]:11434'
Assert-True (Test-LoopbackAddress '::ffff:127.0.0.1') 'IPv4-mapped loopback'
Assert-True (-not (Test-LoopbackAddress '0.0.0.0')) '0.0.0.0 is no loopback'

$head = "router:`n  url: https://router.example:11435`n`nchildren:`n"
$stt = "  - name: stt`n    cmd: 'C:\stt\python.exe'`n    env:`n      - 'OLLAMA_HOST=0.0.0.0:11434'   # unrelated child, must stay`n"
$tail = "`ninstall:`n  service_name: SkirnirAgent`n"

# 1. list form with a value: only that line changes, quotes and comment kept, the other child untouched
$cfg = $head + $stt + "  - name: ollama`n    cmd: 'C:\o\ollama.exe'`n    args: ['serve']`n    env:`n      - 'OLLAMA_HOST=0.0.0.0:11434'   # old`n      - 'OLLAMA_MODELS=D:\m'`n    health_tcp_port: 11434`n" + $tail
$r = Set-ChildOllamaHost $cfg $T
Assert-Equal $r.State 'changed' 'list: state'
Assert-Equal $r.Before '0.0.0.0:11434' 'list: before'
Assert-True ($r.Text.Contains("      - 'OLLAMA_HOST=127.0.0.1:11434'   # old")) 'list: rewritten line keeps quotes and comment'
Assert-True ($r.Text.Contains("      - 'OLLAMA_HOST=0.0.0.0:11434'   # unrelated child, must stay")) 'list: other child unchanged'
Assert-Equal (($r.Text -split "`n").Count) (($cfg -split "`n").Count) 'list: same number of lines'

# 2. already loopback: current, text unchanged
$r2 = Set-ChildOllamaHost $r.Text $T
Assert-Equal $r2.State 'current' 'idempotent'

# 3. no OLLAMA_HOST in the child's env list: inserted after the last entry with the same quoting
$cfg3 = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n    env:`n      - OLLAMA_MODELS=D:\m`n      - OLLAMA_NUM_PARALLEL=2`n    startup_grace_s: 60`n" + $tail
$r3 = Set-ChildOllamaHost $cfg3 $T
Assert-Equal $r3.State 'changed' 'insert: state'
Assert-True ($r3.Text.Contains("      - OLLAMA_NUM_PARALLEL=2`n      - OLLAMA_HOST=127.0.0.1:11434   # TM-11: loopback only`n    startup_grace_s: 60")) 'insert: position and form'

# 4. env as a mapping
$cfg4 = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n    env:`n      OLLAMA_HOST: 0.0.0.0`n      OLLAMA_MODELS: D:\m`n" + $tail
$r4 = Set-ChildOllamaHost $cfg4 $T
Assert-True ($r4.Text.Contains("      OLLAMA_HOST: '127.0.0.1:11434'")) 'map: rewritten and quoted'

# 5. child without env: env block added at the end of the item
$cfg5 = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n    args: [serve]`n" + $tail
$r5 = Set-ChildOllamaHost $cfg5 $T
Assert-True ($r5.Text.Contains("    args: [serve]`n    env:`n      - 'OLLAMA_HOST=127.0.0.1:11434'   # TM-11: loopback only`n`ninstall:")) 'no env: block added inside the item'

# 6. recognised by cmd when the name differs; CRLF kept
$cfg6 = ($head + "  - name: llm`n    cmd: 'C:\Program Files\Ollama\ollama.exe'`n    env:`n      - OLLAMA_HOST=0.0.0.0`n" + $tail) -replace "`n", "`r`n"
$r6 = Set-ChildOllamaHost $cfg6 $T
Assert-Equal $r6.State 'changed' 'by cmd: state'
Assert-True ($r6.Text.Contains("      - OLLAMA_HOST=127.0.0.1:11434`r`n")) 'by cmd: CRLF kept'
Assert-True (-not ($r6.Text -match "[^`r]`n")) 'by cmd: no bare LF introduced'

# 7. refusals: no Ollama child, two Ollama children, inline env, duplicate key
Assert-Equal (Set-ChildOllamaHost ($head + $stt + $tail) $T).State 'refused' 'no ollama child'
$two = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n  - name: ollama2`n    cmd: C:\p\ollama.exe`n" + $tail
Assert-Equal (Set-ChildOllamaHost $two $T).State 'refused' 'two ollama children'
$inline = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n    env: ['OLLAMA_HOST=0.0.0.0']`n" + $tail
Assert-Equal (Set-ChildOllamaHost $inline $T).State 'refused' 'inline env'
$dup = $head + "  - name: ollama`n    cmd: C:\o\ollama.exe`n    env:`n      - OLLAMA_HOST=0.0.0.0`n      - OLLAMA_HOST=::`n" + $tail
Assert-Equal (Set-ChildOllamaHost $dup $T).State 'refused' 'duplicate OLLAMA_HOST'
Assert-Equal (Find-OllamaChild (Split-Lines "router:`n  url: x`n").Lines).Count 0 'no children block'

# 8. agent settings that reach Ollama over the network are found, loopback ones are not
$net = "tunnel:`n  upstream: http://192.0.2.20:11434`nmqtt:`n  ollama_url: 'http://127.0.0.1:11434'`nollama_proxy:`n  upstream: http://[::1]:11434`n"
$found = @(Find-NetworkUpstream $net)
Assert-Equal $found.Count 1 'network upstream: count'
Assert-True ($found[0] -like 'upstream: http://192.0.2.20:11434*') 'network upstream: which'
Assert-Equal @(Find-NetworkUpstream "tunnel:`n  upstream: http://192.0.2.20:8080`n").Count 0 'other port is not Ollama'

# 9. the transcript of an elevated run goes to the agent's log folder (else TEMP), never next to the script: run from a
#    repository checkout it would put computer and user names into the working tree
$svc = '"C:\Program Files\skirnir-agent\skirnir-agent.exe" --config "C:\ProgramData\skirnir-agent\config.yaml"'
Assert-Equal (Split-ServiceCommand $svc).Exe 'C:\Program Files\skirnir-agent\skirnir-agent.exe' 'service command: program'
Assert-Equal (Split-ServiceCommand $svc).Config 'C:\ProgramData\skirnir-agent\config.yaml' 'service command: configuration'
Assert-Equal (Split-ServiceCommand 'C:\agent\skirnir-agent.exe run').Config '' 'service command: no --config'
Assert-Equal (Get-AgentLogFolder $svc) 'C:\ProgramData\skirnir-agent\logs' 'log folder: logs next to the configuration'
Assert-Equal (Get-AgentLogFolder 'C:\agent\skirnir-agent.exe --config D:\node-data\config.yaml') 'D:\node-data\logs' 'log folder: unquoted command line'
Assert-Equal (Get-AgentLogFolder '"C:\agent\skirnir-agent.exe" --config "C:\config.yaml"') '' 'log folder: none for a configuration at a drive root'
Assert-Equal (Get-AgentLogFolder '"C:\agent\skirnir-agent.exe" run') '' 'log folder: none without --config'
Assert-Equal (Get-AgentLogFolder '') '' 'log folder: none without a service'
$source = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'agent' | Join-Path -ChildPath 'Set-OllamaLoopback.ps1')
Assert-True ($source -notmatch 'PSScriptRoot[^\r\n]*\.log') 'no log file next to the script'
Assert-Equal ([regex]::Matches($source, 'Start-Transcript').Count) 1 'Start-Transcript only in Start-RunTranscript'
Assert-True ($source -match "Start-RunTranscript 'Set-OllamaLoopback'") 'the run starts its transcript through Start-RunTranscript'
if ($env:OS -eq 'Windows_NT') {
    # host side, Windows only: a real transcript is written, and outside this checkout
    $repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $log = Start-RunTranscript 'selftest-ollama-loopback'
    if ($log -ne '') { Stop-Transcript | Out-Null }
    Assert-True ($log -ne '' -and (Test-Path -LiteralPath $log)) "transcript written: $log"
    Assert-True (-not $log.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) "transcript outside the checkout ($repo): $log"
    if ($log -ne '' -and (Test-Path -LiteralPath $log)) { Remove-Item -LiteralPath $log }
}

if ($Script:Failures -gt 0) { Write-Host "$Script:Failures failure(s)"; exit 1 }
Write-Host 'selftest_ollama_loopback: all passed'
exit 0
