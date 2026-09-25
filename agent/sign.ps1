<#
    sign.ps1 -- signiert die Agent-Binary (Authenticode) mit dem Code-Signing-Zertifikat des Betreibers.

    Warum: Smart App Control / WDAC blockt unsignierte Binaries ("did not meet the Enterprise signing level"); das
    Selbst-Update tauscht dann nicht, und Doppelklick-Setup auf neuen Knoten scheitert. Smart App Control akzeptiert nur
    Zertifikate von CAs im Microsoft Trusted Root Program (kein self-signed).

    Aufruf (Windows, das Zertifikat liegt im Benutzerspeicher bzw. auf der Karte/im Cloud-CSP):
        powershell -ExecutionPolicy Bypass -File agent\sign.ps1                       # dist\skirnir-agent.exe
        powershell -ExecutionPolicy Bypass -File agent\sign.ps1 -Path pfad\zur.exe -Thumbprint <sha1>
    Thumbprint: Parameter, sonst $env:SKIRNIR_SIGN_THUMBPRINT, sonst AGENT_SIGN_THUMBPRINT aus <skirnir-ops>\deploy.env.
    signtool: Windows SDK (Windows Kits\10\bin\<ver>\x64) oder das NuGet-Paket Microsoft.Windows.SDK.BuildTools, entpackt
    nach <skirnir-ops>\tools\signtool\ (signtool.exe irgendwo darunter). Eine schon gueltig mit demselben Zertifikat
    signierte Datei wird uebersprungen (-Force signiert trotzdem neu).
#>
[CmdletBinding()]
param(
    [string]$Path = (Join-Path $PSScriptRoot 'dist\skirnir-agent.exe'),
    [string]$Thumbprint = $env:SKIRNIR_SIGN_THUMBPRINT,
    [string]$TimestampUrl = 'http://time.certum.pl',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

function Find-OpsDir {
    if ($env:SKIRNIR_OPS -and (Test-Path (Join-Path $env:SKIRNIR_OPS 'deploy.env'))) { return $env:SKIRNIR_OPS }
    $sib = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'skirnir-ops'
    if (Test-Path (Join-Path $sib 'deploy.env')) { return $sib }
    return $null
}

function Find-SignTool {
    $ops = Find-OpsDir
    if ($ops) {
        $t = Get-ChildItem (Join-Path $ops 'tools\signtool') -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName | Select-Object -Last 1
        if ($t) { return $t.FullName }
    }
    $kits = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1
    if ($kits) { return $kits.FullName }
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "signtool.exe nicht gefunden. Windows SDK installieren oder das NuGet-Paket Microsoft.Windows.SDK.BuildTools nach <skirnir-ops>\tools\signtool\ entpacken (Anleitung: skirnir-ops\CODE-SIGNING.md)."
}

if (-not (Test-Path $Path)) { throw "Datei fehlt: $Path" }
if (-not $Thumbprint) {
    $ops = Find-OpsDir
    if ($ops) {
        $line = Get-Content (Join-Path $ops 'deploy.env') | Where-Object { $_ -match '^AGENT_SIGN_THUMBPRINT=' } | Select-Object -First 1
        if ($line) { $Thumbprint = ($line -split '=', 2)[1].Trim().Trim('"') }
    }
}
if (-not $Thumbprint) { throw 'Kein Thumbprint: -Thumbprint, $env:SKIRNIR_SIGN_THUMBPRINT oder AGENT_SIGN_THUMBPRINT in deploy.env setzen.' }
$Thumbprint = $Thumbprint.ToUpper() -replace '[^0-9A-F]', ''

$cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $Thumbprint } | Select-Object -First 1
if (-not $cert) { throw "Kein Code-Signing-Zertifikat mit Thumbprint $Thumbprint im Zertifikatspeicher (CurrentUser/LocalMachine My). Karte gesteckt, SimplySign/SignService gestartet, Zertifikat installiert?" }
if ($cert.NotAfter -lt (Get-Date)) { throw "Zertifikat abgelaufen am $($cert.NotAfter)" }

$sig = Get-AuthenticodeSignature $Path
if (-not $Force -and $sig.Status -eq 'Valid' -and $sig.SignerCertificate.Thumbprint -eq $Thumbprint) {
    Write-Host "schon signiert ($($sig.SignerCertificate.Subject)), uebersprungen: $Path"
    exit 0
}

$signtool = Find-SignTool
Write-Host "signiere $Path mit $($cert.Subject) (laeuft bis $($cert.NotAfter.ToString('yyyy-MM-dd')), signtool $signtool)"
& $signtool sign /sha1 $Thumbprint /fd SHA256 /td SHA256 /tr $TimestampUrl $Path
if ($LASTEXITCODE -ne 0) { throw "signtool meldete Exit-Code $LASTEXITCODE" }

$sig = Get-AuthenticodeSignature $Path
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Thumbprint -ne $Thumbprint) { throw "Signatur nach dem Signieren nicht gueltig: $($sig.Status) $($sig.StatusMessage)" }
Write-Host "OK: $Path signiert von $($sig.SignerCertificate.Subject)$(if ($sig.TimeStamperCertificate) { ', Zeitstempel ' + $sig.TimeStamperCertificate.Subject.Split(',')[0] })"
