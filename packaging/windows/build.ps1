param(
    [switch]$SkipInstall,
    [switch]$SkipInstaller,
    [switch]$SmokeTestInstaller,
    [string]$ExpectedVersion = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

if ($env:OS -ne 'Windows_NT') { throw 'Build on Windows x64 with Python 3.12.' }
if ($SkipInstaller -and $SmokeTestInstaller) {
    throw 'Cannot run -SmokeTestInstaller when -SkipInstaller is specified.'
}

function Run-Python {
    & .\.venv\Scripts\python.exe @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed ($LASTEXITCODE)." }
}

function Find-Iscc {
    if ($env:ISCC_PATH) {
        if (-not (Test-Path $env:ISCC_PATH)) {
            throw "ISCC_PATH does not exist: $($env:ISCC_PATH)"
        }
        return (Resolve-Path $env:ISCC_PATH).Path
    }
    $fromPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    throw 'Inno Setup 6 compiler (ISCC.exe) not found. Install Inno Setup 6, set ISCC_PATH, or pass -SkipInstaller.'
}

if (-not (Test-Path .venv\Scripts\python.exe)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64 from python.org first.' }
}
Run-Python -c "import struct, sys; assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8, 'Python 3.12 x64 is required'"

$version = (& .\.venv\Scripts\python.exe -c "import ast, pathlib; tree = ast.parse(pathlib.Path('src/voicetyping/__init__.py').read_text(encoding='utf-8')); print(next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)))").Trim()
if (-not ($version -match '^[0-9]+\.[0-9]+\.[0-9]+$')) {
    throw "Invalid package version: $version"
}
if ($ExpectedVersion -and $ExpectedVersion -ne $version) {
    throw "Requested version $ExpectedVersion does not match package version $version."
}

$isccPath = $null
if (-not $SkipInstaller) {
    $isccPath = Find-Iscc
}

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

$zipPath = 'dist\VoiceTyping-windows-x64.zip'
Compress-Archive -Path dist\VoiceTyping -DestinationPath $zipPath -Force
$artifactPaths = @($zipPath)

$setupName = "VoiceTyping-$version-windows-x64-setup.exe"
$setupPath = "dist\$setupName"
if (-not $SkipInstaller) {
    if (Test-Path $setupPath) { Remove-Item $setupPath -Force }
    & $isccPath "/DAppVersion=$version" "/DSourceDir=$repoRoot\dist\VoiceTyping" "/DOutputDir=$repoRoot\dist" 'packaging\windows\VoiceTyping.iss'
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed ($LASTEXITCODE)." }
    if (-not (Test-Path $setupPath)) { throw "Expected installer was not created: $setupPath" }
    $artifactPaths += $setupPath
}

$checksumLines = @()
foreach ($artifact in ($artifactPaths | Sort-Object { Split-Path $_ -Leaf })) {
    $hash = (Get-FileHash $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    $leaf = Split-Path $artifact -Leaf
    $checksumLines += "$hash  $leaf"
}
$checksumLines | Set-Content -Encoding ascii dist\SHA256SUMS.txt

foreach ($line in (Get-Content dist\SHA256SUMS.txt)) {
    $parts = $line -split '\s+', 2
    $expectedHash = $parts[0]
    $targetFile = Join-Path 'dist' $parts[1]
    $actualHash = (Get-FileHash $targetFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Checksum verification failed for $targetFile."
    }
}

if ($SmokeTestInstaller) {
    & (Join-Path $PSScriptRoot 'smoke-installer.ps1') -SetupPath $setupPath
    if ($LASTEXITCODE -ne 0) { throw "Installer smoke test failed ($LASTEXITCODE)." }
}

Write-Host ("Built " + ($artifactPaths -join ', ') + ' and passed verification.')
