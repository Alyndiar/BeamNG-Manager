param(
    [ValidateSet("gui", "debug")]
    [string]$Variant = "gui",
    [string]$Version = "dev",
    [string]$CondaEnv = "BeamNG-Manager"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$IconPng = Join-Path $RepoRoot "ui\assets\icons\BeamNG-Manager.png"
$IconIco = Join-Path $RepoRoot "packaging\BeamNG-Manager.ico"

if (-not (Test-Path $IconPng)) {
    throw "Missing icon source: $IconPng"
}

$IconBuildScript = @"
from pathlib import Path
from PySide6.QtGui import QImage

src = Path(r"$IconPng")
dst = Path(r"$IconIco")
img = QImage(str(src))
if img.isNull():
    raise SystemExit(f"Failed to load icon source: {src}")
dst.parent.mkdir(parents=True, exist_ok=True)
if not img.save(str(dst), "ICO"):
    raise SystemExit(f"Failed to write icon file: {dst}")
print(f"Prepared icon: {dst}")
"@

$IconBuildScript | conda run -n $CondaEnv python -

conda run -n $CondaEnv python -m py_compile ui/main_window.py
conda run -n $CondaEnv python -m pytest -q
conda run -n $CondaEnv python -m pip install --disable-pip-version-check pyinstaller

if (Test-Path "build") {
    Remove-Item -Recurse -Force "build"
}
if (Test-Path "dist\BeamNG-Manager.exe") {
    Remove-Item -Force "dist\BeamNG-Manager.exe"
}
if (Test-Path "dist\BeamNG-Manager-debug.exe") {
    Remove-Item -Force "dist\BeamNG-Manager-debug.exe"
}

$env:BEAMNG_BUILD_VARIANT = $Variant
$env:BEAMNG_APP_VERSION = $Version
try {
    conda run -n $CondaEnv python -m PyInstaller packaging/beamng_manager.spec --noconfirm --clean
}
finally {
    Remove-Item Env:BEAMNG_BUILD_VARIANT -ErrorAction SilentlyContinue
    Remove-Item Env:BEAMNG_APP_VERSION -ErrorAction SilentlyContinue
}

$ExeName = if ($Variant -eq "debug") { "BeamNG-Manager-debug.exe" } else { "BeamNG-Manager.exe" }
$ExePath = Join-Path $RepoRoot ("dist\" + $ExeName)
if (-not (Test-Path $ExePath)) {
    throw "Expected output EXE not found: $ExePath"
}

$ReleaseDir = Join-Path $RepoRoot "dist\release"
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

$ArtifactBase = if ($Variant -eq "debug") {
    "BeamNG-Manager-$Version-windows-x64-debug-console"
}
else {
    "BeamNG-Manager-$Version-windows-x64"
}

$ZipPath = Join-Path $ReleaseDir "$ArtifactBase.zip"
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}
Compress-Archive -Path $ExePath -DestinationPath $ZipPath -CompressionLevel Optimal

$Hash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLowerInvariant()
$ShaPath = "$ZipPath.sha256"
"$Hash *$(Split-Path -Leaf $ZipPath)" | Set-Content -Path $ShaPath -Encoding Ascii

Write-Host "Created: $ZipPath"
Write-Host "Checksum: $ShaPath"
