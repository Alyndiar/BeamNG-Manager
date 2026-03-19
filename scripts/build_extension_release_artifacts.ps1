param(
    [string]$OutputDir = "dist/release"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir
} else {
    Join-Path $RepoRoot $OutputDir
}
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

$extensionsDir = Join-Path $outputRoot "extensions"
New-Item -ItemType Directory -Path $extensionsDir -Force | Out-Null

$null = & node --version
if ($LASTEXITCODE -ne 0) {
    throw "Node.js is required to package extension assets."
}

$chromeManifestPath = Join-Path $RepoRoot "integrations\chrome-beamng-manager\manifest.json"
$firefoxManifestPath = Join-Path $RepoRoot "integrations\firefox-beamng-manager\manifest.json"
$chromeOfficialLinkPath = Join-Path $RepoRoot "integrations\chrome-beamng-manager\official_listing_url.txt"
$firefoxOfficialLinkPath = Join-Path $RepoRoot "integrations\firefox-beamng-manager\official_listing_url.txt"

$chromeVersion = [string]((Get-Content -LiteralPath $chromeManifestPath -Raw | ConvertFrom-Json).version)
$firefoxVersion = [string]((Get-Content -LiteralPath $firefoxManifestPath -Raw | ConvertFrom-Json).version)
if ([string]::IsNullOrWhiteSpace($chromeVersion) -or [string]::IsNullOrWhiteSpace($firefoxVersion)) {
    throw "Missing extension version in one or more extension manifest files."
}
if ($chromeVersion -ne $firefoxVersion) {
    throw "Chrome extension version ($chromeVersion) must match Firefox extension version ($firefoxVersion)."
}

if (-not (Test-Path -LiteralPath $chromeOfficialLinkPath)) {
    throw "Missing Chrome official listing URL file: $chromeOfficialLinkPath"
}

$chromePublicLink = (Get-Content -LiteralPath $chromeOfficialLinkPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($chromePublicLink)) {
    throw "Chrome official listing URL is empty in: $chromeOfficialLinkPath"
}
$chromePublicLinkLabel = "Chrome Web Store listing"

if (-not (Test-Path -LiteralPath $firefoxOfficialLinkPath)) {
    throw "Missing Firefox official listing URL file: $firefoxOfficialLinkPath"
}

$firefoxPublicLink = (Get-Content -LiteralPath $firefoxOfficialLinkPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($firefoxPublicLink)) {
    throw "Firefox official listing URL is empty in: $firefoxOfficialLinkPath"
}
$firefoxPublicLinkLabel = "Firefox official listing"

& powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "integrations\chrome-beamng-manager\package_zip.ps1") -OutputDir $extensionsDir
if ($LASTEXITCODE -ne 0) {
    throw "Failed to package Chrome extension."
}
& powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "integrations\firefox-beamng-manager\package_xpi.ps1") -OutputDir $extensionsDir
if ($LASTEXITCODE -ne 0) {
    throw "Failed to package Firefox extension."
}

$chromeZip = Join-Path $extensionsDir "beamng-manager-bridge-chrome-$chromeVersion.zip"
$firefoxXpi = Join-Path $extensionsDir "beamng-manager-bridge-$firefoxVersion-unsigned.xpi"

if (-not (Test-Path -LiteralPath $chromeZip)) {
    throw "Expected Chrome package not found: $chromeZip"
}
if (-not (Test-Path -LiteralPath $firefoxXpi)) {
    throw "Expected Firefox package not found: $firefoxXpi"
}

foreach ($assetPath in @($chromeZip, $firefoxXpi)) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $assetPath).Hash.ToLowerInvariant()
    $shaPath = "$assetPath.sha256"
    "$hash *$(Split-Path -Leaf $assetPath)" | Set-Content -Path $shaPath -Encoding Ascii
}

$linksPath = Join-Path $outputRoot "extension-links.html"
$chromeZipName = [System.IO.Path]::GetFileName($chromeZip)
$html = @"
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>BeamNG-Manager Extension Links</title>
    <style>
      body { font-family: Segoe UI, Arial, sans-serif; margin: 24px; }
      h1 { margin-top: 0; }
    </style>
  </head>
  <body>
    <h1>Extension Links</h1>
    <ul>
      <li>${chromePublicLinkLabel}: <a href="$chromePublicLink">$chromePublicLink</a></li>
      <li>Chrome unpacked package artifact: <code>$chromeZipName</code></li>
      <li>${firefoxPublicLinkLabel}: <a href="$firefoxPublicLink">$firefoxPublicLink</a></li>
    </ul>
  </body>
</html>
"@
$html | Set-Content -LiteralPath $linksPath -Encoding UTF8

Write-Host "Created extension artifacts in: $extensionsDir"
Write-Host "Created extension link metadata: $linksPath"
