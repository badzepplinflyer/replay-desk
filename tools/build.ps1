# Builds dist\ReplayDesk-Setup.exe (run on Windows; used by the GitHub release workflow).
param([string]$Version = "0.0.0-dev")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
New-Item -ItemType Directory -Force build, dist | Out-Null

Write-Host "== Python packages"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt pyinstaller --quiet

Write-Host "== FFmpeg"
$ffDir = "build\ffmpeg\bin"
if (-not (Test-Path "$ffDir\ffmpeg.exe")) {
    New-Item -ItemType Directory -Force $ffDir | Out-Null
    $zip = "build\ffmpeg.zip"
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -UseBasicParsing
    } catch {
        Write-Host "gyan.dev unavailable, using BtbN build"
        Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip -UseBasicParsing
    }
    Expand-Archive $zip -DestinationPath "build\ffmpeg-src" -Force
    Get-ChildItem "build\ffmpeg-src" -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $ffDir
    Remove-Item $zip, "build\ffmpeg-src" -Recurse -Force
}
& "$ffDir\ffmpeg.exe" -hide_banner -version | Select-Object -First 1
$filters = & "$ffDir\ffmpeg.exe" -hide_banner -filters | Out-String
if ($filters -notmatch "ddagrab") { throw "FFmpeg build lacks ddagrab screen capture" }

Write-Host "== Icon"
Copy-Item "$Root\assets\icon.ico" "$Root\build\icon.ico" -Force

Write-Host "== PyInstaller"
python -m PyInstaller --noconfirm --clean --windowed --name ReplayDesk `
    --icon "$Root\build\icon.ico" `
    --add-data "$Root\web;web" `
    --add-data "$Root\build\ffmpeg;ffmpeg" `
    --hidden-import pystray._win32 `
    --collect-submodules send2trash `
    --collect-binaries pyaudiowpatch `
    --distpath "$Root\dist" --workpath "$Root\build\pyi" --specpath "$Root\build" `
    "$Root\app.py"
if (-not (Test-Path "dist\ReplayDesk\ReplayDesk.exe")) { throw "PyInstaller build failed" }

Write-Host "== Installer"
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    choco install innosetup -y --no-progress | Out-Null
}
& $iscc "/DAppVersion=$Version" "installer\ReplayDesk.iss"
if (-not (Test-Path "dist\ReplayDesk-Setup.exe")) { throw "Installer build failed" }
Write-Host "Built dist\ReplayDesk-Setup.exe ($Version)"
