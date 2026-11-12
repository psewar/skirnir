# Self-test for agent/Move-OllamaProtected.ps1 (TM-14, ADR-0085): the pure parts - who could change a path according to
# its owner and access list (the agent's rule), the chain of paths checked, and rewriting only the Ollama child's cmd.
# Runs with Windows PowerShell 5.1 and pwsh 7 on any OS:  pwsh -NoProfile -File test/selftest_ollama_protected.ps1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'agent' | Join-Path -ChildPath 'Move-OllamaProtected.ps1')

$Script:Failures = 0
function Assert-Equal($actual, $expected, [string] $what) {
    if ("$actual" -ne "$expected") { Write-Host "FAIL $what`n  expected: $expected`n  actual:   $actual"; $Script:Failures++ }
}
function Assert-True([bool] $value, [string] $what) { if (-not $value) { Write-Host "FAIL $what"; $Script:Failures++ } }

$sys = 'S-1-5-18'; $adm = 'S-1-5-32-544'; $users = 'S-1-5-32-545'; $user = 'S-1-5-21-1-2-3-1001'
$ti = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
function Rule($sid, $rights, [switch] $Deny, [switch] $InheritOnly) { @{ Sid = $sid; Rights = $rights; Allow = -not $Deny; InheritOnly = [bool]$InheritOnly } }
$read = 0x1200A9        # read and execute
$modify = 0x1301BF      # modify (write, append, delete)
$full = 0x1F01FF

# A program folder like C:\Program Files\Ollama: admins and system full, users read
$protected = @((Rule $sys $full), (Rule $adm $full), (Rule $users $read), (Rule $ti $full))
Assert-Equal (Get-Exposure $adm $protected 'file') '' 'protected file'
Assert-Equal (Get-Exposure $ti $protected 'dir') '' 'protected folder owned by TrustedInstaller'

# The per-user install: the user owns it and has full control
Assert-Equal (Get-Exposure $user @((Rule $sys $full), (Rule $user $full)) 'file') "$user (owner)" 'user owner'
Assert-Equal (Get-Exposure $adm @((Rule $sys $full), (Rule $user $full)) 'file') $user 'user with full control'
Assert-Equal (Get-Exposure $adm @((Rule $users $modify)) 'file') $users 'Users may modify'

# Only the rights that matter for the kind count
$addFile = 0x2
Assert-Equal (Get-Exposure $adm @((Rule $users $addFile)) 'dir') $users 'add file to the program folder'
Assert-Equal (Get-Exposure $adm @((Rule $users $addFile)) 'ancestor') '' 'add file to a folder above: harmless'
Assert-Equal (Get-Exposure $adm @((Rule $users 0x4)) 'ancestor') '' 'create folders in C:\ (Authenticated Users): harmless'
Assert-Equal (Get-Exposure $adm @((Rule $users 0x40)) 'ancestor') $users 'delete child in a folder above: can swap the program folder'
Assert-Equal (Get-Exposure $adm @((Rule $users 0x40000)) 'ancestor') $users 'change permissions of a folder above'
Assert-Equal (Get-Exposure $adm @((Rule $users 0x10000000)) 'file') $users 'GENERIC_ALL'
Assert-Equal (Get-Exposure $adm @((Rule $users 0x20)) 'file') '' 'execute only'

# Inherit-only and deny entries are skipped (deny entries are not weighed: the check may over-report, never under-report)
Assert-Equal (Get-Exposure $adm @((Rule $users $full -InheritOnly)) 'dir') '' 'inherit-only entry applies to children only'
Assert-Equal (Get-Exposure $adm @((Rule $users $full -Deny)) 'dir') '' 'a deny entry grants nothing'
Assert-Equal (Get-Exposure $adm @((Rule 'S-1-3-0' $full)) 'dir') '' 'CREATOR OWNER stands for the owner'

# Chain: program, its folder, every folder above
if ($IsLinux -or $IsMacOS) {
    Write-Host '(path chain: Windows paths split only on Windows - skipped here)'
} else {
    $chain = @(Get-PathChain 'C:\Program Files\Ollama\ollama.exe')
    Assert-Equal ($chain | ForEach-Object { "$($_.Kind):$($_.Path)" }) @('file:C:\Program Files\Ollama\ollama.exe', 'dir:C:\Program Files\Ollama', 'ancestor:C:\Program Files', 'ancestor:C:\') 'path chain'
}

# cmd rewrite: only the Ollama child's line, quotes added for spaces, comment kept, the other child untouched
$head = "router:`n  url: https://router.example:11435`n`nchildren:`n"
$stt = "  - name: stt`n    cmd: 'C:\stt\python.exe'`n"
$tail = "`ninstall:`n  service_name: SkirnirAgent`n"
$cfg = $head + $stt + "  - name: ollama`n    cmd: D:\tools\per-user-install\Ollama\ollama.exe   # per-user`n    args: ['serve']`n" + $tail
$r = Set-ChildCmd $cfg 'C:\Program Files\Ollama\ollama.exe'
Assert-Equal $r.State 'changed' 'cmd: state'
Assert-True ($r.Text.Contains("    cmd: 'C:\Program Files\Ollama\ollama.exe'   # per-user")) 'cmd: quoted, comment kept'
Assert-True ($r.Text.Contains("    cmd: 'C:\stt\python.exe'")) 'cmd: other child unchanged'
Assert-Equal (($r.Text -split "`n").Count) (($cfg -split "`n").Count) 'cmd: same number of lines'
Assert-Equal (Set-ChildCmd $r.Text 'C:\Program Files\Ollama\ollama.exe').State 'current' 'cmd: idempotent'
$crlf = $cfg -replace "`n", "`r`n"
Assert-True ((Set-ChildCmd $crlf 'C:\Program Files\Ollama\ollama.exe').Text.Contains("`r`n")) 'cmd: CRLF kept'
Assert-Equal (Get-ChildCmd ($head + $stt + $tail)).State 'refused' 'no Ollama child: refused'
$two = $head + "  - name: ollama`n    cmd: 'C:\a\ollama.exe'`n    cmd: 'C:\b\ollama.exe'`n" + $tail
Assert-Equal (Get-ChildCmd $two).State 'refused' 'two cmd lines: refused'

if ($Script:Failures -gt 0) { Write-Host "$($Script:Failures) failure(s)"; exit 1 }
Write-Host 'selftest_ollama_protected: all passed'
exit 0
