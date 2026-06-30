$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$AppName = "91wllm-downloader"
$DistDir = Join-Path $PSScriptRoot "dist"
$AppDistDir = Join-Path $DistDir $AppName
$PlaywrightRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"

Write-Host "Installing Python dependencies..."
python -m pip install -r requirements.txt

Write-Host "Installing Playwright Chromium..."
python -m playwright install chromium

$addDataArgs = @()
if (Test-Path $PlaywrightRoot) {
    $addDataArgs += "--add-data"
    $addDataArgs += "$PlaywrightRoot;ms-playwright"
} else {
    Write-Warning "Playwright browser folder was not found: $PlaywrightRoot"
    Write-Warning "The generated app may need Chromium installed on the target machine."
}

Write-Host "Building exe folder..."
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --noconsole `
    --name $AppName `
    --collect-submodules playwright `
    --hidden-import playwright.sync_api `
    @addDataArgs `
    app_gui.py

if (Test-Path $AppDistDir) {
    Copy-Item -Path (Join-Path $PSScriptRoot "crawler_config.py") -Destination $AppDistDir -Force
    Get-ChildItem -Path $PSScriptRoot -Filter "*.md" | Copy-Item -Destination $AppDistDir -Force
    Remove-Item -Path (Join-Path $AppDistDir "app_settings.json") -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Build complete:"
Write-Host $AppDistDir
Write-Host "Give users the whole folder, not only the exe file."
