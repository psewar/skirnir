<#
Moves the Ollama that the agent runs as a child into a folder only administrators can change (TM-14, ADR-0085).
Windows PowerShell 5.1 or 7. Move-OllamaProtected.cmd next to this file starts it with -ExecutionPolicy Bypass for this
one call; without administrator rights the script asks for them (UAC) and continues in an elevated window that waits
for Enter at the end.

  Move-OllamaProtected.cmd                  copy, point the child at the copy, restart, verify
  Move-OllamaProtected.cmd -Check           only report: where the child's Ollama lies and who could change it
  Move-OllamaProtected.cmd -Target <dir>    another protected folder (default C:\Program Files\Ollama)

Why: the agent runs as LocalSystem and starts its children with those rights. The per-user Ollama installer puts
ollama.exe and its libraries under %LOCALAPPDATA%\Programs\Ollama, which the user owns; whatever that user runs could
replace them and would run as the system on the next start. Agent 0.32.0 refuses such a child (ADR-0085).

What it does, each step checked before the next:
  1. Administrator rights; the service SkirnirAgent and its configuration; exactly one children[] entry for Ollama, with
     cmd written on one line.
  2. Who besides SYSTEM, Administrators and TrustedInstaller could change the child's program, its folder or a folder
     above (owner and access list - the same rule as the agent). Nobody: nothing to do (STATE: current).
  3. The target must be protected by the same rule and must not hold another Ollama. The program folder is copied there
     (robocopy, the copy takes the target's permissions; the user's installation stays untouched).
  4. The child's cmd in the agent configuration points at the copy (backup next to it), `check-config` of the installed
     agent accepts it, the service restarts, Ollama answers on 127.0.0.1 from the copy. Otherwise the configuration is
     restored, a copy made by this run is removed, and the service restarts.
A log is written next to this file. The last line is always STATE: moved | current | checked | refused | restored.
Afterwards disable the autostart of the Ollama desktop app; the router's Ollama updates replace the files in the copy.
#>
param(
    [switch] $Check,
    [string] $Target = 'C:\Program Files\Ollama',
    [switch] $Elevated
)
$ErrorActionPreference = 'Stop'
$Script:Opt = @{ Check = [bool]$Check; Target = $Target; Elevated = [bool]$Elevated }
# Find-OllamaChild, Split-Lines, Test-Content, Wait-Loopback helpers: shared with the loopback script (dot-sourcing it
# does not run it). Its parameters would shadow ours, hence the copy in $Script:Opt above.
. (Join-Path $PSScriptRoot 'Set-OllamaLoopback.ps1')
$Elevated = $Script:Opt.Elevated

# ---------------------------------------------------------------- pure helpers (tested by test/selftest_ollama_protected.ps1)

$Script:TrustedSids = @(
    'S-1-5-18',      # LocalSystem
    'S-1-5-32-544',  # Administrators
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',  # TrustedInstaller
    'S-1-3-0',       # CREATOR OWNER (stands for the owner, checked separately)
    'S-1-3-4'        # OWNER RIGHTS
)
# FILE_WRITE_DATA/ADD_FILE 0x2, APPEND_DATA/ADD_SUBDIRECTORY 0x4, DELETE_CHILD 0x40, DELETE 0x10000, WRITE_DAC 0x40000,
# WRITE_OWNER 0x80000, GENERIC_ALL 0x10000000, GENERIC_WRITE 0x40000000 - the masks of the agent (childpath_windows.go)
$Script:ExposureMask = @{
    file     = 0x2 -bor 0x4 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
    dir      = 0x2 -bor 0x4 -bor 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
    ancestor = 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000
}

function Get-Exposure([string] $ownerSid, $rules, [string] $kind) {
    # rules: @{ Sid; Rights (int64); Allow (bool); InheritOnly (bool) }. Returns the SID that could change the object, or ''.
    if ($ownerSid -and $Script:TrustedSids -notcontains $ownerSid) { return "$ownerSid (owner)" }
    $mask = [int64]$Script:ExposureMask[$kind]
    foreach ($r in $rules) {
        if (-not $r.Allow -or $r.InheritOnly) { continue }
        if (([int64]$r.Rights -band $mask) -eq 0) { continue }
        if ($Script:TrustedSids -notcontains $r.Sid) { return $r.Sid }
    }
    return ''
}

function Get-PathChain([string] $exe) {
    # @(@{Path; Kind}) for the program, its folder and every folder above, as the agent checks them
    $out = @(@{ Path = $exe; Kind = 'file' })
    $dir = [System.IO.Path]::GetDirectoryName($exe)
    $out += @{ Path = $dir; Kind = 'dir' }
    $p = [System.IO.Path]::GetDirectoryName($dir)
    while ($p) {
        $out += @{ Path = $p; Kind = 'ancestor' }
        $p = [System.IO.Path]::GetDirectoryName($p)
    }
    return $out
}

function Get-ChildCmd([string] $text) {
    # @{ State = ok|refused; Index; Cmd; Line; Message } for the cmd line of the single Ollama child
    $sp = Split-Lines $text
    $child = Find-OllamaChild $sp.Lines
    if ($child.Count -ne 1) { return @{ State = 'refused'; Message = "children: $($child.Count) entries run Ollama, expected one" } }
    $re = '^(?<p>\s*-?\s*cmd:\s*)(?<q>[''"]?)(?<v>[^''"#]+?)\k<q>\s*(?<c>#.*)?$'
    $hits = @()
    for ($i = $child.Start; $i -lt $child.End; $i++) { if ($sp.Lines[$i] -match $re) { $hits += $i } }
    if ($hits.Count -ne 1) { return @{ State = 'refused'; Message = "child $($child.Name): expected one cmd line, found $($hits.Count)" } }
    $null = $sp.Lines[$hits[0]] -match $re
    return @{ State = 'ok'; Index = $hits[0]; Cmd = $Matches.v.Trim(); Name = $child.Name }
}

function Set-ChildCmd([string] $text, [string] $newCmd) {
    # Returns @{State = changed|current|refused; Text; Before; Message}; only the cmd line of the Ollama child changes
    $c = Get-ChildCmd $text
    if ($c.State -ne 'ok') { return $c }
    if ($c.Cmd -eq $newCmd) { return @{ State = 'current'; Text = $text; Before = $c.Cmd; Message = "child $($c.Name) already runs $newCmd" } }
    $sp = Split-Lines $text
    $re = '^(?<p>\s*-?\s*cmd:\s*)(?<q>[''"]?)(?<v>[^''"#]+?)\k<q>\s*(?<c>#.*)?$'
    $null = $sp.Lines[$c.Index] -match $re
    $q = $Matches.q
    if ($q -eq '') { $q = "'" }   # a path with spaces needs quotes in YAML
    $comment = ''
    if ($Matches.c) { $comment = '   ' + $Matches.c }
    $sp.Lines[$c.Index] = "$($Matches.p)$q$newCmd$q$comment"
    return @{ State = 'changed'; Text = ($sp.Lines -join $sp.NewLine); Before = $c.Cmd; Message = "child $($c.Name): cmd $($c.Cmd) -> $newCmd" }
}

# ---------------------------------------------------------------- host side

function Get-PathExposure([string] $path, [string] $kind) {
    $acl = Get-Acl -LiteralPath $path
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    $rules = @()
    foreach ($r in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        $rules += @{ Sid = $r.IdentityReference.Value; Rights = [int64]$r.FileSystemRights
            Allow = ($r.AccessControlType -eq 'Allow')
            InheritOnly = (($r.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) }
    }
    $sid = Get-Exposure $owner $rules $kind
    if ($sid -eq '') { return '' }
    $plain = $sid -replace ' \(owner\)$', ''
    $name = $plain
    $id = New-Object System.Security.Principal.SecurityIdentifier $plain
    if ($id.IsValidTargetType([System.Security.Principal.NTAccount])) {
        try { $name = $id.Translate([System.Security.Principal.NTAccount]).Value } catch {
            # exception-ok: an orphaned SID has no name; the SID itself is shown
            $name = $plain
        }
    }
    if ($sid.EndsWith('(owner)')) { $name += ' (owner)' }
    return $name
}

function Find-Exposure([string] $exe) {
    # first @{Path; Who} on the chain someone else could change, or $null
    foreach ($s in (Get-PathChain $exe)) {
        if (-not (Test-Path -LiteralPath $s.Path)) { continue }
        $who = Get-PathExposure $s.Path $s.Kind
        if ($who -ne '') { return @{ Path = $s.Path; Who = $who } }
    }
    return $null
}

function Finish([string] $state, [string] $text, [int] $code) {
    Write-Host $text
    Write-Host "STATE: $state"
    if ($Script:Transcript) { Stop-Transcript | Out-Null }
    if ($Elevated) { Read-Host 'Press Enter to close' | Out-Null }
    exit $code
}

function Wait-OllamaFrom([string] $exe) {
    # true when Ollama answers on 127.0.0.1 and the running ollama.exe is the one at $exe
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        $running = @(Get-CimInstance Win32_Process -Filter "Name='ollama.exe'" | Where-Object { $_.ExecutablePath -eq $exe })
        if ($running.Count -eq 0) { continue }
        $answer = $null
        try { $answer = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri 'http://127.0.0.1:11434/api/version' } catch {
            # exception-ok: the HTTP call is the only way to learn whether Ollama answers yet; retried until the deadline
            continue
        }
        if ($answer.StatusCode -eq 200) { return $true }
    }
    return $false
}

function Invoke-Move {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        if ($Elevated) { Finish 'refused' 'Still no administrator rights after elevation.' 4 }
        $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated -Target `"$($Script:Opt.Target -replace '"', '')`""
        if ($Script:Opt.Check) { $argList += ' -Check' }
        Write-Host 'Administrator rights are needed - Windows asks for them now (UAC). The run continues in a new window.'
        try {
            $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
        } catch {
            # exception-ok: a declined UAC prompt is reported only as an error of Start-Process
            Finish 'refused' "Elevation did not happen: $($_.Exception.Message)" 4
        }
        Write-Host "The elevated run ended with exit code $($p.ExitCode); its log is next to this script."
        exit $p.ExitCode
    }
    $Script:Transcript = $false
    $log = Join-Path $PSScriptRoot "Move-OllamaProtected-$($env:COMPUTERNAME)-$(Get-Date -Format yyyyMMdd-HHmmss).log"
    try {
        Start-Transcript -Path $log | Out-Null
        $Script:Transcript = $true
    } catch {
        # exception-ok: whether the script folder is writable shows only on the attempt; the run works without a log
        Write-Host "No log file ($($_.Exception.Message)); continuing without one."
    }
    $targetDir = [System.IO.Path]::GetFullPath($Script:Opt.Target)
    $targetExe = Join-Path $targetDir 'ollama.exe'
    Write-Host "Node $env:COMPUTERNAME  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  target $targetDir"

    # --- 1. agent, configuration, child
    $service = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq 'SkirnirAgent' }
    if (-not $service) { Finish 'refused' 'Service SkirnirAgent not found: Ollama is not an agent child on this machine.' 4 }
    if ($service.PathName -notmatch '^"?(?<exe>[^"]+\.exe)"?\s+--config\s+"?(?<cfg>[^"]+?)"?\s*$') { Finish 'refused' "Service command line not recognised: $($service.PathName)" 4 }
    $agentExe, $cfg = $Matches.exe, $Matches.cfg
    if (-not (Test-Path $agentExe) -or -not (Test-Path $cfg)) { Finish 'refused' "Agent binary or configuration missing: $agentExe / $cfg" 4 }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $cfgText = [System.IO.File]::ReadAllText($cfg, $utf8)
    $child = Get-ChildCmd $cfgText
    if ($child.State -ne 'ok') { Finish 'refused' $child.Message 4 }
    $sourceExe = [Environment]::ExpandEnvironmentVariables($child.Cmd)
    if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) { Finish 'refused' "The child's program does not exist: $sourceExe" 4 }
    $sourceExe = (Resolve-Path -LiteralPath $sourceExe).ProviderPath
    $sourceDir = [System.IO.Path]::GetDirectoryName($sourceExe)
    Write-Host "Agent configuration: $cfg  child $($child.Name): $sourceExe"

    # --- 2. who could change it now
    $now = Find-Exposure $sourceExe
    if ($null -eq $now) { Finish 'current' "Nobody besides the system and the administrators can change $sourceExe or a folder above it." 0 }
    Write-Host "Can be changed: $($now.Path) by $($now.Who)"
    if ($sourceDir -ieq $targetDir) { Finish 'refused' "The child already runs from $targetDir, but that folder is not protected - fix its permissions by hand." 4 }

    # --- 3. the target
    $created = -not (Test-Path -LiteralPath $targetDir)
    if (-not $created -and (Get-ChildItem -LiteralPath $targetDir -Force | Select-Object -First 1)) {
        if (-not (Test-Path -LiteralPath $targetExe)) { Finish 'refused' "$targetDir exists and holds something else; choose another -Target." 4 }
        $have = (& $targetExe --version 2>&1 | Out-String).Trim()
        $want = (& $sourceExe --version 2>&1 | Out-String).Trim()
        if ($have -ne $want) { Finish 'refused' "$targetDir already holds another Ollama ($have, the child runs $want); remove it or choose another -Target." 4 }
        Write-Host "$targetDir already holds the same Ollama ($have); it is used as it is."
    }
    $parent = [System.IO.Path]::GetDirectoryName($targetDir)
    $targetWho = Find-Exposure (Join-Path $parent 'probe.exe')
    if ($null -ne $targetWho) { Finish 'refused' "The target is not protected either: $($targetWho.Path) can be changed by $($targetWho.Who)." 4 }
    if ($Script:Opt.Check) { Finish 'checked' "Would copy $sourceDir to $targetDir and point the child at $targetExe." 0 }

    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $backup = "$cfg.before-ollama-move-$stamp"
    # Each step runs only while $why is empty; a failed step names itself in $why and all take the same way back.
    $why = ''
    if ($created -or -not (Test-Path -LiteralPath $targetExe)) {
        Write-Host "Copying $sourceDir -> $targetDir ..."
        $ErrorActionPreference = 'Continue'
        & robocopy.exe $sourceDir $targetDir /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NP | Out-Null
        $rc = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($rc -ge 8) { $why = "robocopy failed with exit code $rc" }
    }
    if ($why -eq '') {
        $after = Find-Exposure $targetExe
        if ($null -ne $after) { $why = "the copy can be changed: $($after.Path) by $($after.Who)" }
    }
    # --- 4. configuration, check, restart, verify
    $plan = $null
    if ($why -eq '') {
        $plan = Set-ChildCmd $cfgText $targetExe
        if ($plan.State -eq 'refused') { $why = $plan.Message }
    }
    if ($why -eq '') {
        try {
            Copy-Item -LiteralPath $cfg -Destination $backup
            [System.IO.File]::WriteAllText($cfg, $plan.Text, $utf8)
            Write-Host "Agent configuration: $($plan.Message)"
        } catch {
            # exception-ok: file system (permissions, locks) - only the write itself can tell
            $why = "configuration not written: $($_.Exception.Message)"
        }
    }
    if ($why -eq '') {
        $ErrorActionPreference = 'Continue'
        $checked = (& $agentExe check-config --config $cfg 2>&1 | ForEach-Object { "$_" }) -join ' '
        $checkCode = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($checkCode -ne 0) { $why = "check-config: $checked" }
    }
    if ($why -eq '') {
        Write-Host 'Restarting service SkirnirAgent (Ollama and the other children restart with it) ...'
        try { Restart-Service -Name SkirnirAgent } catch {
            # exception-ok: service control reports a failed stop or start only as an error
            $why = "service restart: $($_.Exception.Message)"
        }
    }
    if ($why -eq '' -and -not (Wait-OllamaFrom $targetExe)) { $why = "Ollama did not answer on 127.0.0.1 from $targetExe within 90 s" }
    if ($why -ne '') {
        if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath $backup -Destination $cfg -Force }
        if ($created -and (Test-Path -LiteralPath $targetDir)) { Remove-Item -LiteralPath $targetDir -Recurse -Force -ErrorAction SilentlyContinue }
        Restart-Service -Name SkirnirAgent -ErrorAction SilentlyContinue
        Finish 'restored' "Failed: $why. Configuration restored$(if (Test-Path -LiteralPath $backup) { " from $backup" }), service restarted." 1
    }
    Write-Host "Ollama runs from $targetExe; configuration backup: $backup"
    Finish 'moved' "Disable the Ollama desktop app's autostart; its copy under $sourceDir is no longer used by the agent." 0
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Move }
