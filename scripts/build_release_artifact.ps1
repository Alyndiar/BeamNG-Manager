param(
    [ValidateSet("gui", "debug")]
    [string]$Variant = "gui",
    [string]$Version = "dev",
    [string]$CondaEnv = "BeamNG-Manager",
    [switch]$IncludeRawExe
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$IconIco = Join-Path $RepoRoot "ui\assets\icons\BeamNG-Manager.ico"

if (-not (Test-Path $IconIco)) {
    throw "Missing icon file: $IconIco"
}

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

if ($IncludeRawExe) {
    $RawExePath = Join-Path $ReleaseDir "$ArtifactBase.exe"
    if (Test-Path $RawExePath) {
        Remove-Item -Force $RawExePath
    }
    Copy-Item -LiteralPath $ExePath -Destination $RawExePath -Force

    $RawHash = (Get-FileHash -Algorithm SHA256 $RawExePath).Hash.ToLowerInvariant()
    $RawShaPath = "$RawExePath.sha256"
    "$RawHash *$(Split-Path -Leaf $RawExePath)" | Set-Content -Path $RawShaPath -Encoding Ascii

    Write-Host "Created: $RawExePath"
    Write-Host "Checksum: $RawShaPath"
}
