<#
.SYNOPSIS
    Builds, tests and packages Media Reader x. No admin rights required.

.DESCRIPTION
    1. Creates .venv with Python 3.14 (if missing) and installs pinned dependencies.
    2. Runs the pytest suite (blocking: a failure stops the build).
    3. Generates a CycloneDX SBOM, including the native libraries inside the wheels (tools\sbom_native.py).
    4. Audits it: pip-audit on the pinned requirements, grype on the SBOM (tools\vuln_scan.py). A High or
       Critical finding without a valid entry in packaging\vuln-exceptions.json stops the build.
       Both need network access to their vulnerability databases.
    5. Freezes the app with PyInstaller (one-folder build).
    6. Runs the frozen exe's --self-test to prove every codec and every content check was bundled.
    7. Generates THIRD_PARTY_NOTICES.txt, the per-user Inno Setup installer and a portable zip.
    8. Writes SHA256 checksums. Everything lands in .\dist (the SharePoint folder).

.PARAMETER SkipTests
    Skip pytest (not recommended; use only when iterating on packaging).

.PARAMETER SkipAudit
    Skip pip-audit and grype, for example without network access. The build prints a warning:
    never share such a build.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
#>
[CmdletBinding()]
param([switch]$SkipTests, [switch]$SkipAudit)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

$Version = (Select-String -Path "$Root\src\mediareaderx\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Media Reader x $Version" -ForegroundColor Green

Step 'Python environment'
# Python 3.14 (per-user install). 3.12 gets no more Windows installers, so its security fixes never ship.
$PyBase = if ($env:MRX_PYTHON) { $env:MRX_PYTHON } else { "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" }
$Py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    if (-not (Test-Path $PyBase)) { throw "Python 3.14 not found at $PyBase. Install it per user, or set MRX_PYTHON." }
    Invoke-Checked $PyBase @('-m', 'venv', "$Root\.venv")
}
$PyVersion = (& $Py -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($PyVersion -ne '3.14') { throw ".venv uses Python $PyVersion, the build needs 3.14. Rename .venv and run again." }
Invoke-Checked $Py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', "$Root\requirements-dev.txt")
Invoke-Checked $Py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-deps', '-e', $Root)

if (-not $SkipTests) {
    Step 'Tests'
    Invoke-Checked $Py @('-m', 'pytest', '-q', '-p', 'no:cacheprovider')
}

if (Test-Path "$Root\build") { Remove-Item -Recurse -Force "$Root\build" }
New-Item -ItemType Directory "$Root\build\security" | Out-Null
$Sbom = "$Root\build\security\sbom.cdx.json"

Step 'SBOM (Python packages and the native libraries inside them)'
Invoke-Checked $Py @('-m', 'cyclonedx_py', 'requirements', "$Root\requirements.txt", '--of', 'JSON', '-o', $Sbom)
Invoke-Checked $Py @("$Root\tools\sbom_native.py", $Sbom)

if ($SkipAudit) {
    Write-Host "`n!!! SECURITY AUDIT SKIPPED (-SkipAudit). Do not share this build. !!!" -ForegroundColor Red
} else {
    Step 'Vulnerability audit (pip-audit, grype)'
    # Pinned files only: --disable-pip --no-deps audits exactly what ships, without resolving or downloading.
    foreach ($req in @("$Root\requirements.txt", "$Root\requirements-dev.txt")) {
        Invoke-Checked $Py @('-m', 'pip_audit', '--disable-pip', '--no-deps', '--strict', '--progress-spinner', 'off', '-r', $req)
    }
    Invoke-Checked $Py @("$Root\tools\vuln_scan.py", $Sbom, "$Root\build\security")
}

Step 'Icon, notices and installer associations'
if (-not (Test-Path "$Root\src\mediareaderx\assets\app.ico")) {
    Invoke-Checked $Py @("$Root\tools\make_icon.py", "$Root\src\mediareaderx\assets\app.ico")
}
Invoke-Checked $Py @("$Root\tools\gen_notices.py", "$Root\THIRD_PARTY_NOTICES.txt")
Invoke-Checked $Py @("$Root\tools\gen_iss_associations.py", "$Root\packaging\associations.iss")

# --- GPL and LGPL source archives (tools\fetch_sources.py, packaging\sources.json) ---------------------
# x265 is GPL; libheif, libde265, LibRaw and FFmpeg are LGPL. Their source ships next to the binaries, in
# dist\source (copied there in the checksum step). Downloaded once into
# %LOCALAPPDATA%\MediaReaderX-build\sources and checked against the pinned SHA-256 on every build.
Step 'GPL and LGPL source archives'
Invoke-Checked $Py @("$Root\tools\fetch_sources.py", "$Root\build\source")
# --- end of the source archives step ---------------------------------------------------------------

Step 'PyInstaller'
Invoke-Checked $Py @('-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN',
    '--distpath', "$Root\build\dist", '--workpath', "$Root\build\work", "$Root\packaging\MediaReaderX.spec")
$Exe = "$Root\build\dist\MediaReaderX\MediaReaderX.exe"

Step 'Self-test of the frozen app'
$Report = "$env:LOCALAPPDATA\MediaReaderX\selftest.txt"
Remove-Item $Report -ErrorAction SilentlyContinue  # never print an old report as if it were this run's
$proc = Start-Process -FilePath $Exe -ArgumentList '--self-test' -Wait -PassThru -WindowStyle Hidden
if (Test-Path $Report) { Get-Content $Report } else { Write-Host 'The self-test wrote no report.' -ForegroundColor Red }
if ($proc.ExitCode -ne 0) { throw "Frozen self-test failed (exit $($proc.ExitCode)). See $env:LOCALAPPDATA\MediaReaderX\logs" }

Step 'Installer'
$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw 'Inno Setup 6 not found. Install per-user: winget install JRSoftware.InnoSetup --scope user' }
$Dist = "$Root\dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory $Dist | Out-Null
Invoke-Checked $Iscc @('/Q', "/DAppVersion=$Version", "$Root\packaging\MediaReaderX.iss")

Step 'Portable zip'
Copy-Item "$Root\LICENSE" "$Root\build\dist\MediaReaderX\LICENSE.txt"
Copy-Item "$Root\THIRD_PARTY_NOTICES.txt" "$Root\build\dist\MediaReaderX\"
Compress-Archive -Path "$Root\build\dist\MediaReaderX" -DestinationPath "$Dist\MediaReaderX-$Version-Portable.zip" -CompressionLevel Optimal

Step 'SBOM and checksums'
Copy-Item $Sbom "$Dist\MediaReaderX-$Version-sbom.cdx.json"
Copy-Item "$Root\docs\INSTALL.txt" "$Dist\INSTALL.txt"
Copy-Item "$Root\THIRD_PARTY_NOTICES.txt" $Dist
Copy-Item -Recurse "$Root\build\source" "$Dist\source"  # from the source archives step
# Recursive, so the source archives are covered too (paths like source/x265_4.3.tar.gz).
$Sums = Get-ChildItem $Dist -File -Recurse | Where-Object FullName -ne "$Dist\SHA256SUMS.txt" | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.FullName.Substring($Dist.Length + 1).Replace('\', '/')
}
if (-not $Sums) { throw 'No files found to checksum in dist' }
$Sums | Set-Content -Encoding ascii "$Dist\SHA256SUMS.txt"

Step 'Done'
Get-ChildItem $Dist | Format-Table Name, @{ n = 'Size (MB)'; e = { '{0:N1}' -f ($_.Length / 1MB) } } -AutoSize
Write-Host "Share the folder: $Dist" -ForegroundColor Green
