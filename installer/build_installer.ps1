# Compile SPLAT!Gui (PyInstaller) puis les deux installeurs (Inno Setup) :
#   dist\SPLAT!Gui-Setup-<version>.exe          application seule
#   dist\SPLAT!Gui-Setup-<version>-complet.exe  avec SPLAT! x64 / x86, leurs DLL et gnuplot
# Usage : powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 [-SkipPyInstaller]
param([switch]$SkipPyInstaller)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

$version = (Select-String -Path "$root\splatgui\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

if (-not $SkipPyInstaller) {
    & "$root\.venv\Scripts\pyinstaller.exe" "$root\SplatGui.spec" --noconfirm --distpath "$root\dist" --workpath "$root\build"
    if ($LASTEXITCODE) { throw "Échec de PyInstaller ($LASTEXITCODE)" }
}

# Contenu de l'installeur complet (installé au préalable par la fenêtre Pré-requis).
foreach ($required in "bin\x64\splat.exe", "bin\x86\splat.exe", "deps\x64\msys-2.0.dll",
                      "deps\x86\libstdc++-6.dll", "gnuplot\bin\gnuplot.exe") {
    if (-not (Test-Path "$root\$required")) { throw "$required introuvable (Fichier → Pré-requis)" }
}

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 introuvable (winget install JRSoftware.InnoSetup)" }

& $iscc "/DAppVersion=$version" "$PSScriptRoot\SplatGui.iss"
if ($LASTEXITCODE) { throw "Échec d'Inno Setup ($LASTEXITCODE)" }
& $iscc "/DAppVersion=$version" "/DComplet" "$PSScriptRoot\SplatGui.iss"
if ($LASTEXITCODE) { throw "Échec d'Inno Setup, installeur complet ($LASTEXITCODE)" }
Write-Host "Installeurs : $root\dist\SPLAT!Gui-Setup-$version.exe"
Write-Host "              $root\dist\SPLAT!Gui-Setup-$version-complet.exe"
