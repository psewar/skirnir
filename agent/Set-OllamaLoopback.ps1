<#
Binds Ollama on THIS node to loopback (127.0.0.1:11434), so it is reachable only through the agent and the router
(TM-11, ADR-0072). Windows PowerShell 5.1 or 7. Set-OllamaLoopback.cmd next to this file starts it with
-ExecutionPolicy Bypass for this one call; without administrator rights the script asks for them (UAC) and continues in
an elevated window that waits for Enter at the end.

  Set-OllamaLoopback.cmd                    change what is needed, restart Ollama, verify
  Set-OllamaLoopback.cmd -Check             only report: how Ollama runs, where it listens, what would change
  Set-OllamaLoopback.cmd -Force "<reason>"  continue although other machines are connected to Ollama right now

How Ollama runs is detected, not asked:
  * child of the agent (children[] entry for ollama.exe in the agent configuration): OLLAMA_HOST in that child's env is
    set to 127.0.0.1:11434 (added if missing - the service would otherwise inherit a machine-wide value), check-config
    with the installed agent, service restart.
  * Ollama desktop app (tray) of a signed-in user: OLLAMA_HOST of that user (and machine-wide, if set to a network
    address) becomes 127.0.0.1:11434; the app is restarted in that user's session.

Checks before acting: administrator rights; exactly one way Ollama runs; no agent upstream pointing at Ollama over a
network address (the agent would lose its own Ollama); no other machine connected to port 11434 right now (else
-Force with a reason). After the restart: Ollama answers on 127.0.0.1 and listens on loopback only, else the backup is
restored. Then inbound firewall rules that only opened Ollama (port 11434 alone, or the Ollama program) are disabled
(not deleted). The log goes to the agent's log folder (logs next to its configuration, by default
C:\ProgramData\skirnir-agent\logs; without an agent the TEMP folder of the elevated account), never next to this file;
the line "Log:" names it. The last line is always STATE: changed | current | checked | refused | restored.
#>
param(
    [switch] $Check,
    [string] $Force = '',
    [switch] $Elevated
)
$ErrorActionPreference = 'Stop'
$Script:Target = '127.0.0.1:11434'
$Script:Port = 11434

# ---------------------------------------------------------------- pure helpers (tested by test/selftest_ollama_loopback.ps1)

function Get-HostOfValue([string] $value) {
    # OLLAMA_HOST forms: "0.0.0.0", "0.0.0.0:11434", "http://host:11434", "[::]:11434", "::", ":11434"
    $v = $value.Trim().Trim("'").Trim('"')
    $v = $v -replace '^[a-zA-Z][a-zA-Z0-9+.-]*://', ''
    $v = ($v -split '/')[0]
    if ($v -match '^\[(?<h>[^\]]*)\](:\d+)?$') { return $Matches.h }
    if ($v -match '^(?<h>[^:]*):\d+$') { return $Matches.h }
    return $v
}

function Test-LoopbackAddress([string] $address) {
    $a = $address.Trim().Trim('[', ']')
    if ($a -eq '::1' -or $a -eq 'localhost') { return $true }
    if ($a -match '^::ffff:(?<v4>[0-9.]+)$') { $a = $Matches.v4 }
    return [bool]($a -match '^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
}

function Test-LoopbackValue([string] $value) {
    # unset means Ollama's default 127.0.0.1; an empty host (":11434") would mean all addresses
    if ($null -eq $value -or $value.Trim() -eq '') { return $true }
    $h = Get-HostOfValue $value
    if ($h -eq '') { return $false }
    return (Test-LoopbackAddress $h)
}

function Split-Lines([string] $text) {
    $nl = "`n"
    if ($text.Contains("`r`n")) { $nl = "`r`n" }
    return @{ Lines = [System.Collections.Generic.List[string]]($text -split [regex]::Escape($nl)); NewLine = $nl }
}

function Get-Indent([string] $line) { return ($line.Length - $line.TrimStart(' ').Length) }

function Test-Content([string] $line) { $t = $line.Trim(); return ($t -ne '' -and -not $t.StartsWith('#')) }

function Find-OllamaChild([System.Collections.Generic.List[string]] $lines) {
    # Returns @{Start; End (exclusive); KeyIndent; Name} of the children[] item that runs Ollama, @{Count=n} otherwise.
    $top = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^children:\s*(#.*)?$') { $top = $i; break } }
    if ($top -lt 0) { return @{ Count = 0 } }
    $items = @()
    $itemIndent = -1
    $end = $lines.Count
    for ($i = $top + 1; $i -lt $lines.Count; $i++) {
        $l = $lines[$i]
        if (-not (Test-Content $l)) { continue }
        $ind = Get-Indent $l
        $dash = $l.TrimStart(' ').StartsWith('-')
        if ($itemIndent -lt 0) {
            if (-not $dash) { $end = $i; break }
            $itemIndent = $ind
        }
        if ($ind -lt $itemIndent -or ($ind -eq $itemIndent -and -not $dash)) { $end = $i; break }
        if ($ind -eq $itemIndent -and $dash) { $items += $i }
    }
    $found = @()
    for ($k = 0; $k -lt $items.Count; $k++) {
        $s = $items[$k]
        $e = $end
        if ($k + 1 -lt $items.Count) { $e = $items[$k + 1] }
        while ($e -gt $s + 1 -and -not (Test-Content $lines[$e - 1])) { $e-- }
        $block = ($lines.GetRange($s, $e - $s)) -join "`n"
        $name = ''
        if ($block -match '(?m)^\s*-?\s*name:\s*[''"]?(?<n>[^''"#\s]+)') { $name = $Matches.n }
        $isOllama = $name -match '^ollama' -or $block -match '(?m)^\s*-?\s*cmd:\s*[''"]?[^''"#]*[\\/]ollama(\.exe)?[''"]?\s*(#.*)?$'
        if ($isOllama) {
            $m = [regex]::Match($lines[$s], '^(\s*-\s+)')
            $found += @{ Start = $s; End = $e; KeyIndent = $m.Groups[1].Value.Length; Name = $name }
        }
    }
    if ($found.Count -ne 1) { return @{ Count = $found.Count } }
    $r = $found[0]
    $r.Count = 1
    return $r
}

function Set-ChildOllamaHost([string] $text, [string] $target) {
    # Returns @{State = changed|current|refused; Text; Before; Message}
    $sp = Split-Lines $text
    $lines = $sp.Lines
    $child = Find-OllamaChild $lines
    if ($child.Count -ne 1) { return @{ State = 'refused'; Message = "children: $($child.Count) entries run Ollama, expected one" } }
    $listRe = '^(?<p>\s*-\s*)(?<q>[''"]?)OLLAMA_HOST=(?<v>.*?)\k<q>\s*(?<c>#.*)?$'
    $mapRe = '^(?<p>\s*)OLLAMA_HOST:\s*(?<q>[''"]?)(?<v>.*?)\k<q>\s*(?<c>#.*)?$'
    $hits = @()
    for ($i = $child.Start; $i -lt $child.End; $i++) {
        if ($lines[$i] -match $listRe -or $lines[$i] -match $mapRe) { $hits += $i }
        if ($lines[$i] -match '^\s*env:\s*[\[{]') { return @{ State = 'refused'; Message = "env of child $($child.Name) is written inline ([...] or {...}); change it by hand" } }
    }
    if ($hits.Count -gt 1) { return @{ State = 'refused'; Message = "OLLAMA_HOST appears $($hits.Count) times in child $($child.Name)" } }
    if ($hits.Count -eq 1) {
        $i = $hits[0]
        $l = $lines[$i]
        $list = $l -match $listRe
        if (-not $list) { $null = $l -match $mapRe }
        $before = $Matches.v
        if ($before -eq $target) { return @{ State = 'current'; Before = $before; Text = $text; Message = "child $($child.Name) already has OLLAMA_HOST=$target" } }
        $comment = ''
        if ($Matches.c) { $comment = '   ' + $Matches.c }
        if ($list) { $lines[$i] = "$($Matches.p)$($Matches.q)OLLAMA_HOST=$target$($Matches.q)$comment" }
        else { $q = $Matches.q; if ($q -eq '') { $q = "'" }; $lines[$i] = "$($Matches.p)OLLAMA_HOST: $q$target$q$comment" }
        return @{ State = 'changed'; Before = $before; Text = ($lines -join $sp.NewLine); Message = "child $($child.Name): OLLAMA_HOST $before -> $target" }
    }
    # not set in the child: add it (a machine-wide OLLAMA_HOST would otherwise reach the child through the service)
    $envLine = -1
    for ($i = $child.Start; $i -lt $child.End; $i++) { if ($lines[$i] -match '^\s*-?\s*env:\s*(#.*)?$') { $envLine = $i; break } }
    if ($envLine -lt 0) {
        $pad = ' ' * $child.KeyIndent
        $lines.Insert($child.End, "$pad  - 'OLLAMA_HOST=$target'   # TM-11: loopback only")
        $lines.Insert($child.End, "${pad}env:")
        return @{ State = 'changed'; Before = '(unset)'; Text = ($lines -join $sp.NewLine); Message = "child $($child.Name): env with OLLAMA_HOST=$target added" }
    }
    $last = -1
    $first = ''
    for ($i = $envLine + 1; $i -lt $child.End; $i++) {
        if (-not (Test-Content $lines[$i])) { continue }
        if ((Get-Indent $lines[$i]) -lt (Get-Indent $lines[$envLine]) -or ($lines[$i] -notmatch '^\s*-\s' -and (Get-Indent $lines[$i]) -le (Get-Indent $lines[$envLine]))) { break }
        if ($first -eq '') { $first = $lines[$i] }
        $last = $i
    }
    if ($last -lt 0) { return @{ State = 'refused'; Message = "env of child $($child.Name) is empty or unreadable; change it by hand" } }
    if ($first -match '^(?<p>\s*-\s*)(?<q>[''"]?)[A-Za-z_][A-Za-z0-9_]*=') {
        $new = "$($Matches.p)$($Matches.q)OLLAMA_HOST=$target$($Matches.q)   # TM-11: loopback only"
    } elseif ($first -match '^(?<p>\s*)[A-Za-z_][A-Za-z0-9_]*:') {
        $new = "$($Matches.p)OLLAMA_HOST: '$target'   # TM-11: loopback only"
    } else { return @{ State = 'refused'; Message = "env of child $($child.Name) has an unknown form; change it by hand" } }
    $lines.Insert($last + 1, $new)
    return @{ State = 'changed'; Before = '(unset)'; Text = ($lines -join $sp.NewLine); Message = "child $($child.Name): OLLAMA_HOST=$target added" }
}

function Find-NetworkUpstream([string] $text) {
    # agent settings that reach Ollama's port over a non-loopback address: they would break with a loopback bind
    $out = @()
    foreach ($m in [regex]::Matches($text, '(?m)^\s*(?<k>upstream|ollama_url|listen_upstream):\s*[''"]?(?<u>https?://(?<h>\[[^\]]+\]|[^/:''"\s]+):(?<p>\d+))')) {
        if ($m.Groups['p'].Value -eq "$Script:Port" -and -not (Test-LoopbackAddress $m.Groups['h'].Value)) { $out += "$($m.Groups['k'].Value): $($m.Groups['u'].Value)" }
    }
    return $out
}

function Split-ServiceCommand([string] $pathName) {
    # The agent service's command line, "<exe>" --config "<file>". Returns @{Exe; Config}, both '' when not recognised.
    if ("$pathName" -match '^"?(?<exe>[^"]+\.exe)"?\s+--config\s+"?(?<cfg>[^"]+?)"?\s*$') { return @{ Exe = $Matches.exe; Config = $Matches.cfg } }
    return @{ Exe = ''; Config = '' }
}

function Get-AgentLogFolder([string] $servicePathName) {
    # The agent's default log folder: "logs" next to its configuration (applyDefaults in agent/config.go). The agent
    # restricts the configuration folder to SYSTEM, Administrators and its service account (applyRules in
    # agent/install_windows.go), and "logs" inherits that. '' when the command line names no configuration folder, and
    # for a configuration at a drive root (C:\logs would be a folder any user may have created). Windows paths are taken
    # apart as text, so the self-test gives the same answer on every OS.
    $cfg = (Split-ServiceCommand $servicePathName).Config
    if ($cfg -match '^(?<dir>.*[^\\/:])[\\/][^\\/]+$') { return "$($Matches.dir)\logs" }
    return ''
}

# ---------------------------------------------------------------- host side

function Get-TranscriptFolder {
    # The transcript names this computer, user accounts, configuration paths and remote addresses. It goes where only
    # administrators read it and never into the folder of this script: run from a repository checkout, it would land in
    # the working tree, one "git add" away from a public commit. The agent's log folder is chosen over a "logging.dir"
    # of the configuration because the agent itself restricts that folder; without an agent (or with that folder
    # missing) the TEMP folder of the elevated account takes it. Directory.Exists, not Test-Path: without administrator
    # rights Test-Path fails with "access denied" on that folder, Directory.Exists answers false.
    $service = Get-CimInstance Win32_Service -Filter "Name='SkirnirAgent'"
    if ($service) {
        $folder = Get-AgentLogFolder $service.PathName
        if ($folder -ne '' -and [System.IO.Directory]::Exists($folder)) { return $folder }
    }
    return [System.IO.Path]::GetTempPath()
}

function Start-RunTranscript([string] $name) {
    # Starts the transcript of an elevated run and returns its file, '' when none could be started (the run goes on).
    # Shared with Move-OllamaProtected.ps1, which dot-sources this file. The computer name comes from the system, not from
    # %COMPUTERNAME%, which the caller's environment could set to a value with path separators.
    $path = Join-Path (Get-TranscriptFolder) "$name-$([Environment]::MachineName)-$(Get-Date -Format yyyyMMdd-HHmmss).log"
    try {
        Start-Transcript -Path $path | Out-Null
    } catch {
        # exception-ok: whether the folder takes the file shows only on the attempt; the run works without a log
        Write-Host "No log file ($($_.Exception.Message)); continuing without one."
        return ''
    }
    return $path
}

function Finish([string] $state, [string] $text, [int] $code) {
    Write-Host $text
    if ($Script:Transcript) { Write-Host "Log: $Script:Transcript" }
    Write-Host "STATE: $state"
    if ($Script:Transcript) { Stop-Transcript | Out-Null }
    if ($Elevated) { Read-Host 'Press Enter to close' | Out-Null }
    exit $code
}

function Get-Listeners {
    return @(Get-NetTCPConnection -State Listen -LocalPort $Script:Port -ErrorAction SilentlyContinue)
}

function Wait-Loopback {
    # true when Ollama answers on 127.0.0.1 and every listener on the port is a loopback address
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        $ls = Get-Listeners
        if ($ls.Count -eq 0) { continue }
        $open = @($ls | Where-Object { -not (Test-LoopbackAddress $_.LocalAddress) })
        if ($open.Count -gt 0) { continue }
        $answer = $null
        try {
            $answer = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$($Script:Port)/api/version"
        } catch {
            # exception-ok: the HTTP call is the only way to learn whether Ollama answers yet; retried until the deadline
            continue
        }
        if ($answer.StatusCode -eq 200) { return $true }
    }
    return $false
}

function Get-OllamaFirewallRules {
    # enabled inbound allow rules that open Ollama: port 11434 alone or the Ollama program (Only), or 11434 among others
    $ports = @{}
    foreach ($f in @(Get-NetFirewallPortFilter -All -ErrorAction SilentlyContinue)) { $ports[$f.InstanceID] = @($f.LocalPort | ForEach-Object { "$_" }) }
    $programs = @{}
    foreach ($f in @(Get-NetFirewallApplicationFilter -All -ErrorAction SilentlyContinue)) { $programs[$f.InstanceID] = "$($f.Program)" }
    $rules = @()
    foreach ($r in @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue)) {
        $p = @($ports[$r.Name])
        $program = "$($programs[$r.Name])"
        $onlyPort = $p.Count -eq 1 -and $p[0] -eq "$Script:Port"
        $ollamaProgram = $program -match '(?i)[\\/](ollama|ollama app)\.exe$'
        if ($onlyPort -or $ollamaProgram) { $rules += @{ Rule = $r; Only = $true; Why = "port $($p -join ',') program $program" } }
        elseif ($p -contains "$Script:Port") { $rules += @{ Rule = $r; Only = $false; Why = "ports $($p -join ',')" } }
    }
    return $rules
}

function Invoke-Main {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        if ($Elevated) { Finish 'refused' 'Still no administrator rights after elevation.' 4 }
        $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated"
        if ($Check) { $argList += ' -Check' }
        if ($Force -ne '') { $argList += " -Force `"$($Force -replace '"', '')`"" }
        Write-Host 'Administrator rights are needed - Windows asks for them now (UAC). The run continues in a new window.'
        try {
            $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
        } catch {
            # exception-ok: a declined UAC prompt is reported only as an error of Start-Process
            Finish 'refused' "Elevation did not happen: $($_.Exception.Message)" 4
        }
        Write-Host "The elevated run ended with exit code $($p.ExitCode); its window named the log file (agent log folder, without an agent the elevated account's TEMP folder)."
        exit $p.ExitCode
    }
    $Script:Transcript = Start-RunTranscript 'Set-OllamaLoopback'
    Write-Host "Node $env:COMPUTERNAME  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  target OLLAMA_HOST=$Script:Target"

    # --- what is there
    $listeners = Get-Listeners
    $open = @($listeners | Where-Object { -not (Test-LoopbackAddress $_.LocalAddress) })
    Write-Host ("Listening on port {0}: {1}" -f $Script:Port, ((@($listeners | ForEach-Object { $_.LocalAddress }) | Sort-Object -Unique) -join ', '))
    $remote = @(Get-NetTCPConnection -State Established -LocalPort $Script:Port -ErrorAction SilentlyContinue | Where-Object { -not (Test-LoopbackAddress $_.RemoteAddress) })
    $machineValue = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine')
    Write-Host "Machine-wide OLLAMA_HOST: $(if ($machineValue) { $machineValue } else { '(unset)' })"

    $service = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq 'SkirnirAgent' }
    $exe = ''; $cfg = ''; $cfgText = ''; $child = @{ Count = 0 }
    if ($service) {
        $command = Split-ServiceCommand $service.PathName
        if ($command.Config -eq '') { Finish 'refused' "Service command line not recognised: $($service.PathName)" 4 }
        $exe, $cfg = $command.Exe, $command.Config
        if (-not (Test-Path $exe) -or -not (Test-Path $cfg)) { Finish 'refused' "Agent binary or configuration missing: $exe / $cfg" 4 }
        $utf8 = New-Object System.Text.UTF8Encoding $false
        $cfgText = [System.IO.File]::ReadAllText($cfg, $utf8)
        $child = Find-OllamaChild (Split-Lines $cfgText).Lines
        Write-Host "Agent configuration: $cfg (Ollama children: $($child.Count))"
        $bad = @(Find-NetworkUpstream $cfgText)
        if ($bad.Count -gt 0) { Finish 'refused' ("The agent reaches Ollama over a network address - with a loopback bind it would lose it. Change to http://127.0.0.1:$($Script:Port) first: " + ($bad -join '; ')) 4 }
    } else {
        Write-Host 'Service SkirnirAgent not found (no agent on this machine).'
    }
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='ollama app.exe' OR Name='ollama.exe'")
    $tray = @($procs | Where-Object { $_.Name -eq 'ollama app.exe' })
    $servers = @($procs | Where-Object { $_.Name -eq 'ollama.exe' -and $_.CommandLine -match '\sserve\b' })
    Write-Host "Running: $($tray.Count) Ollama app, $($servers.Count) ollama serve"

    if ($child.Count -gt 1) { Finish 'refused' 'More than one agent child runs Ollama; change the configuration by hand.' 4 }
    $mode = ''
    if ($child.Count -eq 1) {
        $mode = 'child'
        if ($tray.Count -gt 0) { Write-Host 'Note: the Ollama desktop app runs as well; it starts its own server. Remove it from autostart.' }
    } elseif ($tray.Count -gt 0 -or $servers.Count -gt 0) {
        $mode = 'app'
    } else {
        Finish 'refused' 'Ollama runs neither as an agent child nor as an app of a signed-in user. Start Ollama and run again.' 4
    }
    Write-Host "Ollama runs as: $(if ($mode -eq 'child') { 'child of the agent' } else { 'desktop app / own server of a user' })"

    # --- plan the change
    $plan = $null
    $owner = $null
    if ($mode -eq 'child') {
        $plan = Set-ChildOllamaHost $cfgText $Script:Target
        if ($plan.State -eq 'refused') { Finish 'refused' $plan.Message 4 }
        Write-Host "Agent configuration: $($plan.Message)"
    } else {
        $p0 = @($tray + $servers)[0]
        $sid = (Invoke-CimMethod -InputObject $p0 -MethodName GetOwnerSid).Sid
        $who = Invoke-CimMethod -InputObject $p0 -MethodName GetOwner
        $owner = @{ Sid = $sid; User = "$($who.Domain)\$($who.User)"; Path = $p0.ExecutablePath; IsTray = ($tray.Count -gt 0) }
        $userKey = "Registry::HKEY_USERS\$sid\Environment"
        $userValue = $null
        if (Test-Path $userKey) { $userValue = (Get-ItemProperty -Path $userKey).OLLAMA_HOST }
        $owner.UserValue = $userValue
        Write-Host "Ollama belongs to $($owner.User); its OLLAMA_HOST: $(if ($userValue) { $userValue } else { '(unset)' })"
    }
    $machineOpen = -not (Test-LoopbackValue $machineValue)
    $userOpen = ($mode -eq 'app') -and -not (Test-LoopbackValue $owner.UserValue)
    $needs = ($open.Count -gt 0)
    if ($mode -eq 'child') { $needs = $needs -or $plan.State -eq 'changed' }
    else { $needs = $needs -or $machineOpen -or $userOpen }
    if ($mode -eq 'child' -and $plan.State -eq 'current' -and $open.Count -gt 0) {
        $holders = (@($open | ForEach-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName }) | Sort-Object -Unique) -join ', '
        Finish 'refused' "The agent child is already set to loopback, but $holders listens on a network address - stop that program (e.g. the Ollama desktop app) and run again." 4
    }

    if ($remote.Count -gt 0) {
        Write-Host ('Connected to Ollama from other machines right now: ' + ((@($remote | ForEach-Object { "$($_.RemoteAddress):$($_.RemotePort)" })) -join ', '))
    }
    $rules = @(Get-OllamaFirewallRules)
    foreach ($r in $rules) { Write-Host ("Firewall rule {0} '{1}' ({2}){3}" -f $r.Rule.Name, $r.Rule.DisplayName, $r.Why, $(if ($r.Only) { ' - only for Ollama' } else { ' - also other ports, stays' })) }

    if ($Check) {
        Finish 'checked' "Would change: $(if ($needs) { 'yes' } else { 'nothing' })" 0
    }
    if (-not $needs) {
        Finish 'current' 'Ollama already listens on loopback only and is configured for it.' 0
    }
    if ($remote.Count -gt 0 -and $Force.Trim() -eq '') {
        Finish 'refused' 'Other machines use Ollama directly right now; they would lose it. Point them at the router, or run again with -Force "<reason>".' 3
    }
    if ($Force.Trim() -ne '') { Write-Host "Continuing despite remote connections: $Force" }

    # --- act, verify, restore on failure
    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    if ($mode -eq 'child') {
        $backup = "$cfg.before-ollama-loopback-$stamp"
        Copy-Item -Path $cfg -Destination $backup
        $ok = $false
        $why = ''
        try {
            [System.IO.File]::WriteAllText($cfg, $plan.Text, (New-Object System.Text.UTF8Encoding $false))
            $ErrorActionPreference = 'Continue'
            $checked = (& $exe check-config --config $cfg 2>&1 | ForEach-Object { "$_" }) -join ' '
            $checkCode = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            if ($checkCode -ne 0) { $why = "check-config: $checked" }
            else {
                Write-Host 'Restarting service SkirnirAgent (Ollama and the other children restart with it) ...'
                Restart-Service -Name SkirnirAgent
                $ok = Wait-Loopback
                if (-not $ok) { $why = 'Ollama did not answer on loopback-only within 90 s' }
            }
        } catch {
            # exception-ok: file system and service control; every failure takes the same way back
            $why = $_.Exception.Message
        }
        if (-not $ok) {
            Copy-Item -Path $backup -Destination $cfg -Force
            Restart-Service -Name SkirnirAgent -ErrorAction SilentlyContinue
            Finish 'restored' "Failed: $why. Configuration restored from $backup, service restarted." 1
        }
        Write-Host "Agent configuration changed; backup: $backup"
    } else {
        $userKey = "Registry::HKEY_USERS\$($owner.Sid)\Environment"
        $machineBefore = $machineValue
        $userBefore = $owner.UserValue
        $ok = $false
        $why = ''
        try {
            if ($machineOpen) { [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $Script:Target, 'Machine'); Write-Host "Machine-wide OLLAMA_HOST: $machineBefore -> $Script:Target" }
            if ($userOpen) {
                Set-ItemProperty -Path $userKey -Name OLLAMA_HOST -Value $Script:Target
                Write-Host "OLLAMA_HOST of $($owner.User): $(if ($userBefore) { $userBefore } else { '(unset)' }) -> $Script:Target"
            }
            Write-Host "Restarting Ollama in the session of $($owner.User) ..."
            Get-CimInstance Win32_Process -Filter "Name='ollama app.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            Get-CimInstance Win32_Process -Filter "Name='ollama.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 2
            $taskName = "SkirnirOllamaLoopback-$stamp"
            $arg = ''
            if (-not $owner.IsTray) { $arg = 'serve' }
            $action = if ($arg) { New-ScheduledTaskAction -Execute $owner.Path -Argument $arg } else { New-ScheduledTaskAction -Execute $owner.Path }
            $principal = New-ScheduledTaskPrincipal -UserId $owner.User -LogonType Interactive
            Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force | Out-Null
            Start-ScheduledTask -TaskName $taskName
            $ok = Wait-Loopback
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            if (-not $ok) { $why = 'Ollama did not answer on loopback-only within 90 s' }
        } catch {
            # exception-ok: registry, process and task scheduler calls; every failure takes the same way back
            $why = $_.Exception.Message
        }
        if (-not $ok) {
            if ($machineOpen) { [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $machineBefore, 'Machine') }
            if ($userOpen) { Set-ItemProperty -Path $userKey -Name OLLAMA_HOST -Value $userBefore }
            Finish 'restored' "Failed: $why. Previous OLLAMA_HOST values restored; start Ollama again from the Start menu." 1
        }
    }
    $now = (@(Get-Listeners | ForEach-Object { $_.LocalAddress }) | Sort-Object -Unique) -join ', '
    Write-Host "Ollama answers on 127.0.0.1 and listens on: $now"

    foreach ($r in @($rules | Where-Object { $_.Only })) {
        Disable-NetFirewallRule -Name $r.Rule.Name
        Write-Host "Firewall rule disabled: '$($r.Rule.DisplayName)' (back: Enable-NetFirewallRule -Name '$($r.Rule.Name)')"
    }
    Finish 'changed' 'Ollama is reachable on this machine only; the router reaches it through the agent.' 0
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Main }
