<#
.SYNOPSIS
  Build airplay-windows: the app folder (PyInstaller), a portable zip, and the installer (Inno Setup).

.EXAMPLE
  .\scripts\build.ps1                 # everything; needs Inno Setup 6 (ISCC.exe) for the installer
  .\scripts\build.ps1 -SkipInstaller  # app folder and zip only
#>
param(
    [switch]$SkipInstaller,
    [string]$Python
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Python) {
    $Python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

Write-Host "== Preparing icon and version resource"
$version = (& $Python packaging\prepare.py).Trim()
if ($LASTEXITCODE -ne 0) { throw "packaging\prepare.py failed" }
Write-Host "   version $version"

Write-Host "== Building the app folder with PyInstaller"
Invoke-Checked $Python @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist",
                         "--workpath", "build\pyinstaller", "packaging\airplay-windows.spec")

Write-Host "== Checking the build"
$cli = "dist\airplay-windows\airplay-windows-cli.exe"
$reported = (& $cli --version).Trim()
if ($reported -ne $version) { throw "airplay-windows-cli.exe reports '$reported', expected '$version'" }

Write-Host "== Writing the portable zip"
$zip = "dist\airplay-windows-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\airplay-windows\*" -DestinationPath $zip

if (-not $SkipInstaller) {
    Write-Host "== Building the installer with Inno Setup"
    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) { throw "ISCC.exe (Inno Setup 6) not found. Install it (choco install innosetup) or pass -SkipInstaller." }
    Invoke-Checked $iscc @("/Qp", "/DAppVersion=$version", "packaging\installer.iss")
}

Write-Host "== Done"
Get-ChildItem dist -File | ForEach-Object { Write-Host ("   {0}  ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB)) }
