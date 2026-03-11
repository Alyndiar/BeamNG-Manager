param(
    [string]$OutputDir = "dist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$integrationsDir = Split-Path -Parent $scriptDir
$buildScriptPath = Join-Path $integrationsDir "build-manifest.js"
$generatedManifestPath = Join-Path (Join-Path $integrationsDir "dist\chrome") "manifest.json"
$manifestPath = Join-Path $scriptDir "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "manifest.json not found in $scriptDir"
}
if (-not (Test-Path -LiteralPath $buildScriptPath)) {
    throw "build-manifest.js not found in $integrationsDir"
}

& node $buildScriptPath "chrome"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build chrome manifest"
}
if (-not (Test-Path -LiteralPath $generatedManifestPath)) {
    throw "Generated chrome manifest not found at $generatedManifestPath"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$version = [string]$manifest.version
if ([string]::IsNullOrWhiteSpace($version)) {
    throw "Missing version in manifest.json"
}

$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir
} else {
    Join-Path $scriptDir $OutputDir
}
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

$zipName = "beamng-manager-bridge-chrome-$version.zip"
$zipPath = Join-Path $outputRoot $zipName
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$exclude = @(
    ".git",
    "dist",
    "*.zip",
    "*.xpi"
)

$files = Get-ChildItem -LiteralPath $scriptDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($scriptDir.Length).TrimStart('\\')
    foreach ($pattern in $exclude) {
        if ($relative -like $pattern -or $relative -like "$pattern\\*") {
            return $false
        }
    }
    return $true
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($scriptDir.Length).TrimStart('\\').Replace('\\', '/')
        $sourcePath = if ($relative -ieq "manifest.json") { $generatedManifestPath } else { $file.FullName }
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $sourcePath, $relative) | Out-Null
    }
}
finally {
    $zip.Dispose()
}

Write-Host "Created Chrome extension zip: $zipPath"
