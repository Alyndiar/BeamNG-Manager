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
$firefoxDownloadLinkPath = Join-Path $RepoRoot "integrations\firefox-beamng-manager\latest_unpublished_download_url.txt"

$chromeVersion = [string]((Get-Content -LiteralPath $chromeManifestPath -Raw | ConvertFrom-Json).version)
$firefoxVersion = [string]((Get-Content -LiteralPath $firefoxManifestPath -Raw | ConvertFrom-Json).version)
if ([string]::IsNullOrWhiteSpace($chromeVersion) -or [string]::IsNullOrWhiteSpace($firefoxVersion)) {
    throw "Missing extension version in one or more extension manifest files."
}

if (-not (Test-Path -LiteralPath $firefoxDownloadLinkPath)) {
    throw "Missing Firefox unpublished download URL file: $firefoxDownloadLinkPath"
}
$firefoxDownloadLink = (Get-Content -LiteralPath $firefoxDownloadLinkPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($firefoxDownloadLink)) {
    throw "Firefox unpublished download URL is empty: $firefoxDownloadLinkPath"
}

$linkVersion = ""
$match = [System.Text.RegularExpressions.Regex]::Match(
    $firefoxDownloadLink,
    "beamng_manager_bridge-(?<version>\d+\.\d+\.\d+)\.xpi",
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
if ($match.Success) {
    $linkVersion = [string]$match.Groups["version"].Value
}
if (-not [string]::IsNullOrWhiteSpace($linkVersion) -and $linkVersion -ne $firefoxVersion) {
    throw (
        "Firefox extension manifest is $firefoxVersion but configured unpublished AMO link is $linkVersion. " +
        "Please provide the updated AMO download URL before release."
    )
}

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

$linksPath = Join-Path $outputRoot "extension-links.md"
@(
    "# Extension Links"
    ""
    "- Chrome: use the attached ZIP artifact (`$([System.IO.Path]::GetFileName($chromeZip))`) with Developer Mode / Load unpacked flow."
    "- Firefox (unpublished listing direct download): $firefoxDownloadLink"
) | Set-Content -LiteralPath $linksPath -Encoding UTF8

Write-Host "Created extension artifacts in: $extensionsDir"
Write-Host "Created extension link metadata: $linksPath"
