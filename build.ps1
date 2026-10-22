<#
.SYNOPSIS
    Builds, tests and packages Media Reader x. No admin rights required.

.DESCRIPTION
    1. Creates .venv (if missing) and installs pinned dependencies.
    2. Runs the pytest suite (blocking: a failure stops the build).
    3. Freezes the app with PyInstaller (one-folder build).
    4. Runs the frozen exe's --self-test to prove every codec was bundled.
    5. Generates THIRD_PARTY_NOTICES.txt and a CycloneDX SBOM.
    6. Compiles the per-user Inno Setup installer and a portable zip.
    7. Writes SHA256 checksums. Everything lands in .\dist (the SharePoint folder).

.PARAMETER SkipTests
    Skip pytest (not recommended; use only when iterating on packaging).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
#>
[CmdletBinding()]
param([switch]$SkipTests)

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
$Py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Invoke-Checked 'python' @('-m', 'venv', "$Root\.venv") }
Invoke-Checked $Py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', "$Root\requirements-dev.txt")
Invoke-Checked $Py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-deps', '-e', $Root)

if (-not $SkipTests) {
    Step 'Tests'
    Invoke-Checked $Py @('-m', 'pytest', '-q', '-p', 'no:cacheprovider')
}

Step 'Icon, notices and installer associations'
if (-not (Test-Path "$Root\src\mediareaderx\assets\app.ico")) {
    Invoke-Checked $Py @("$Root\tools\make_icon.py", "$Root\src\mediareaderx\assets\app.ico")
}
Invoke-Checked $Py @("$Root\tools\gen_notices.py", "$Root\THIRD_PARTY_NOTICES.txt")
Invoke-Checked $Py @("$Root\tools\gen_iss_associations.py", "$Root\packaging\associations.iss")

Step 'PyInstaller'
if (Test-Path "$Root\build") { Remove-Item -Recurse -Force "$Root\build" }
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
Invoke-Checked $Py @('-m', 'cyclonedx_py', 'requirements', "$Root\requirements.txt", '--of', 'JSON',
    '-o', "$Dist\MediaReaderX-$Version-sbom.cdx.json")
Copy-Item "$Root\docs\INSTALL.txt" "$Dist\INSTALL.txt"
Copy-Item "$Root\THIRD_PARTY_NOTICES.txt" $Dist
$Sums = Get-ChildItem $Dist -File | Where-Object Name -ne 'SHA256SUMS.txt' | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
}
if (-not $Sums) { throw 'No files found to checksum in dist' }
$Sums | Set-Content -Encoding ascii "$Dist\SHA256SUMS.txt"

Step 'Done'
Get-ChildItem $Dist | Format-Table Name, @{ n = 'Size (MB)'; e = { '{0:N1}' -f ($_.Length / 1MB) } } -AutoSize
Write-Host "Share the folder: $Dist" -ForegroundColor Green
