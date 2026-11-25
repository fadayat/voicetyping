param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

if ($env:OS -ne 'Windows_NT') { throw 'Build on Windows x64 with Python 3.12.' }
function Run-Python {
    & .\.venv\Scripts\python.exe @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed ($LASTEXITCODE)." }
}

if (-not (Test-Path .venv\Scripts\python.exe)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64 from python.org first.' }
}
Run-Python -c "import struct, sys; assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8, 'Python 3.12 x64 is required'"
if (-not $SkipInstall) {
    Run-Python -m pip install '.[windows]' 'pyinstaller>=6.11,<7'
}

Run-Python -m unittest discover -s tests/windows -v
Run-Python -m compileall -q src\voicetyping packaging\windows
Run-Python -m PyInstaller --noconfirm --clean --windowed --onedir --name VoiceTyping `
    --paths src `
    --collect-all sounddevice --collect-all soundfile `
    --hidden-import keyring.backends.Windows --hidden-import pystray._win32 `
    packaging\windows\launcher.py

$selfTestReport = 'dist\SELFTEST.json'
if (Test-Path $selfTestReport) { Remove-Item $selfTestReport -Force }
$executable = (Resolve-Path 'dist\VoiceTyping\VoiceTyping.exe').Path
$selfTestProcess = Start-Process -FilePath $executable `
    -ArgumentList @('--self-test', '--report', $selfTestReport) `
    -WorkingDirectory $repoRoot -PassThru
if (-not $selfTestProcess.WaitForExit(60000)) {
    Stop-Process -Id $selfTestProcess.Id -Force -ErrorAction SilentlyContinue
    throw 'Packaged self-test timed out after 60 seconds.'
}
$selfTestProcess.WaitForExit()
if ($selfTestProcess.ExitCode -ne 0) { throw "Packaged self-test failed ($($selfTestProcess.ExitCode))." }
if (-not (Test-Path $selfTestReport)) { throw 'Packaged self-test did not create dist\SELFTEST.json.' }
if ((Get-Content $selfTestReport -Raw | ConvertFrom-Json).success -ne $true) {
    throw 'Packaged self-test report is not successful.'
}

$geminiSmokeReport = 'dist\GEMINI_SMOKE.json'
if (Test-Path $geminiSmokeReport) { Remove-Item $geminiSmokeReport -Force }
if ($env:GEMINI_API_KEY) {
    try {
        $geminiProcess = Start-Process -FilePath $executable `
            -ArgumentList @('--gemini-smoke', '--audio', 'packaging\windows\gemini-smoke.wav', '--report', $geminiSmokeReport) `
            -WorkingDirectory $repoRoot -PassThru
        if (-not $geminiProcess.WaitForExit(60000)) {
            Stop-Process -Id $geminiProcess.Id -Force -ErrorAction SilentlyContinue
            throw 'Packaged Gemini smoke test timed out after 60 seconds.'
        }
        $geminiProcess.WaitForExit()
    } finally {
        Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
    }
    if ($geminiProcess.ExitCode -ne 0) { throw "Packaged Gemini smoke test failed ($($geminiProcess.ExitCode))." }
    if (-not (Test-Path $geminiSmokeReport)) { throw 'Packaged Gemini smoke test did not create its JSON report.' }
    if ((Get-Content $geminiSmokeReport -Raw | ConvertFrom-Json).success -ne $true) {
        throw 'Packaged Gemini smoke report is not successful.'
    }
}

Copy-Item README.md, PRIVACY.md, LICENSE -Destination dist\VoiceTyping
Copy-Item docs\WINDOWS.md -Destination dist\VoiceTyping
Run-Python packaging\windows\collect_licenses.py dist\VoiceTyping\THIRD_PARTY_LICENSES
Run-Python -m pip freeze | Set-Content -Encoding utf8 dist\VoiceTyping\DEPENDENCIES.txt
Compress-Archive -Path dist\VoiceTyping -DestinationPath dist\VoiceTyping-windows-x64.zip -Force
$hash = (Get-FileHash dist\VoiceTyping-windows-x64.zip -Algorithm SHA256).Hash.ToLower()
"$hash  VoiceTyping-windows-x64.zip" | Set-Content -Encoding ascii dist\SHA256SUMS.txt

Write-Host 'Built dist\VoiceTyping-windows-x64.zip and passed the packaged self-test.'
