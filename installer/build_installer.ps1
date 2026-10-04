# Compile Splat!Gui (PyInstaller) puis l'installeur (Inno Setup) : dist\SplatGui-Setup-<version>.exe
# Usage : powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 [-SkipPyInstaller]
param([switch]$SkipPyInstaller)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

$version = (Select-String -Path "$root\splatgui\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

if (-not $SkipPyInstaller) {
    & "$root\.venv\Scripts\pyinstaller.exe" "$root\SplatGui.spec" --noconfirm --distpath "$root\dist" --workpath "$root\build"
    if ($LASTEXITCODE) { throw "Échec de PyInstaller ($LASTEXITCODE)" }
}

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 introuvable (winget install JRSoftware.InnoSetup)" }

& $iscc "/DAppVersion=$version" "$PSScriptRoot\SplatGui.iss"
if ($LASTEXITCODE) { throw "Échec d'Inno Setup ($LASTEXITCODE)" }
Write-Host "Installeur : $root\dist\SplatGui-Setup-$version.exe"
