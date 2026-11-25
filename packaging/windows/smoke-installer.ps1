param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Installer smoke test must run on Windows.' }

$resolvedSetup = (Resolve-Path $SetupPath).Path
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) {
    throw "Expected Python environment at $pythonExe."
}

$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runValueName = 'VoiceTyping'
$hadOriginalRunValue = $false
$originalRunValue = $null
if (Test-Path $runKeyPath) {
    $existingProps = Get-ItemProperty -Path $runKeyPath -Name $runValueName -ErrorAction SilentlyContinue
    if ($null -ne $existingProps) {
        $hadOriginalRunValue = $true
        $originalRunValue = $existingProps.$runValueName
        Remove-ItemProperty -Path $runKeyPath -Name $runValueName -Force
    }
}

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$smokeRoot = Join-Path $localAppData ("voicetyping-installer-smoke-" + [guid]::NewGuid().ToString('N'))
$installDir = Join-Path $smokeRoot 'App'
$selfTestReport = Join-Path $smokeRoot 'INSTALLED_SELFTEST.json'
$mutexReadyFile = Join-Path $smokeRoot 'mutex.ready'
$mutexStopFile = Join-Path $smokeRoot 'mutex.stop'
$mutexScript = Join-Path $smokeRoot 'hold_mutex.py'
$mutexProcess = $null

function Invoke-BoundedProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [int]$TimeoutMs = 120000
    )
    $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -PassThru
    if (-not $proc.WaitForExit($TimeoutMs)) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        throw "Process timed out after ${TimeoutMs}ms: $FilePath"
    }
    $proc.WaitForExit()
    return $proc.ExitCode
}

New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null

try {
    # 1. Silent install into isolated per-user directory.
    $installCode = Invoke-BoundedProcess -FilePath $resolvedSetup -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SP-',
        "`"/DIR=$installDir`""
    )
    if ($installCode -ne 0) {
        throw "Silent install failed with exit code $installCode."
    }

    # 2. Verify installed files.
    $expectedFiles = @(
        'VoiceTyping.exe',
        'unins000.exe',
        'README.md',
        'PRIVACY.md',
        'LICENSE',
        'WINDOWS.md',
        'DEPENDENCIES.txt',
        'THIRD_PARTY_LICENSES\PACKAGES.csv'
    )
    foreach ($relative in $expectedFiles) {
        $target = Join-Path $installDir $relative
        if (-not (Test-Path $target)) {
            throw "Installed file missing: $target"
        }
    }

    # 3. Run packaged self-test from the installed executable.
    $installedExe = Join-Path $installDir 'VoiceTyping.exe'
    $selfTestCode = Invoke-BoundedProcess -FilePath $installedExe -ArgumentList @(
        '--self-test',
        '--report',
        $selfTestReport
    ) -TimeoutMs 60000
    if ($selfTestCode -ne 0) {
        throw "Installed self-test failed with exit code $selfTestCode."
    }
    if (-not (Test-Path $selfTestReport)) {
        throw 'Installed self-test did not create its JSON report.'
    }
    if ((Get-Content $selfTestReport -Raw | ConvertFrom-Json).success -ne $true) {
        throw 'Installed self-test report is not successful.'
    }

    # 4. Verify installation did not register startup Run value.
    if (Test-Path $runKeyPath) {
        $afterInstallRun = Get-ItemProperty -Path $runKeyPath -Name $runValueName -ErrorAction SilentlyContinue
        if ($null -ne $afterInstallRun) {
            throw 'Installer unexpectedly created HKCU Run startup entry.'
        }
    }

    # 5. Confirm silent reinstall is rejected while Local\VoiceTyping.Windows.v1 is held.
    @'
import ctypes
from ctypes import wintypes
import pathlib
import sys
import time

ready_path = pathlib.Path(sys.argv[1])
stop_path = pathlib.Path(sys.argv[2])
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel.CreateMutexW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]

ctypes.set_last_error(0)
handle = kernel.CreateMutexW(None, False, 'Local\\VoiceTyping.Windows.v1')
err = ctypes.get_last_error()
if not handle or err == 183:
    raise SystemExit(f'Failed to acquire test mutex (err={err})')
try:
    ready_path.write_text('ready', encoding='utf-8')
    while not stop_path.exists():
        time.sleep(0.05)
finally:
    kernel.CloseHandle(handle)
'@ | Set-Content -Encoding utf8 $mutexScript

    $mutexProcess = Start-Process -FilePath $pythonExe -ArgumentList @($mutexScript, $mutexReadyFile, $mutexStopFile) -PassThru
    $deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path $mutexReadyFile)) {
        if ($mutexProcess.HasExited) {
            throw "Mutex helper exited prematurely with code $($mutexProcess.ExitCode)."
        }
        if ((Get-Date) -gt $deadline) {
            throw 'Timed out waiting for mutex helper.'
        }
        Start-Sleep -Milliseconds 50
    }

    $blockedCode = Invoke-BoundedProcess -FilePath $resolvedSetup -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SP-',
        "`"/DIR=$installDir`""
    )
    if ($blockedCode -eq 0) {
        throw 'Silent reinstall succeeded even though Local\VoiceTyping.Windows.v1 mutex was held.'
    }
    if (-not (Test-Path $installedExe)) {
        throw 'Blocked reinstall damaged the existing VoiceTyping.exe.'
    }

    # Release mutex and wait for helper exit.
    'stop' | Set-Content -Encoding ascii $mutexStopFile
    if (-not $mutexProcess.WaitForExit(15000)) {
        Stop-Process -Id $mutexProcess.Id -Force -ErrorAction SilentlyContinue
        throw 'Mutex helper did not exit after stop signal.'
    }
    $mutexProcess.WaitForExit()
    if ($mutexProcess.ExitCode -ne 0) {
        throw "Mutex helper exited with error code $($mutexProcess.ExitCode)."
    }
    $mutexProcess = $null

    # 6. Seed exact app-owned startup Run value, confirm reinstall preserves it, then verify uninstall removes both Run value and directory.
    if (-not (Test-Path $runKeyPath)) {
        New-Item -Path $runKeyPath -Force | Out-Null
    }
    $expectedRunCmd = "`"$installedExe`" --hidden"
    Set-ItemProperty -Path $runKeyPath -Name $runValueName -Value $expectedRunCmd

    $reinstallCode = Invoke-BoundedProcess -FilePath $resolvedSetup -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SP-',
        "`"/DIR=$installDir`""
    )
    if ($reinstallCode -ne 0) {
        throw "Reinstall after mutex release failed with exit code $reinstallCode."
    }
    if (-not (Test-Path $installedExe)) {
        throw 'VoiceTyping.exe missing after reinstall.'
    }
    $postUpgradeRun = Get-ItemProperty -Path $runKeyPath -Name $runValueName -ErrorAction SilentlyContinue
    if ($null -eq $postUpgradeRun -or $postUpgradeRun.$runValueName -ne $expectedRunCmd) {
        throw 'Reinstall/upgrade did not preserve the existing HKCU Run startup value.'
    }

    # 7. Uninstall silently and verify Run value and directory are gone.

    $uninstaller = Join-Path $installDir 'unins000.exe'
    $uninstallCode = Invoke-BoundedProcess -FilePath $uninstaller -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART'
    )
    if ($uninstallCode -ne 0) {
        throw "Silent uninstall failed with exit code $uninstallCode."
    }

    # Inno Setup's uninstaller spawns a temporary Stage-2 process so it can delete
    # unins000.exe and run usPostUninstall asynchronously after Stage-1 exits.
    $uninstallDeadline = (Get-Date).AddSeconds(20)
    $remainingRun = $null
    while ((Get-Date) -lt $uninstallDeadline) {
        $dirExists = Test-Path $installDir
        $remainingRun = if (Test-Path $runKeyPath) {
            Get-ItemProperty -Path $runKeyPath -Name $runValueName -ErrorAction SilentlyContinue
        } else {
            $null
        }
        if (-not $dirExists -and $null -eq $remainingRun) {
            break
        }
        Start-Sleep -Milliseconds 100
    }

    if ($null -ne $remainingRun) {
        throw "Uninstall did not remove app-owned Run value: $($remainingRun.$runValueName)"
    }
    if (Test-Path $installDir) {
        throw "Installation directory still exists after uninstall: $installDir"
    }

    Write-Host 'Installer smoke test passed: install, self-test, mutex block, reinstall, Run cleanup, and uninstall.'
} finally {
    if ($null -ne $mutexProcess -and -not $mutexProcess.HasExited) {
        'stop' | Set-Content -Encoding ascii $mutexStopFile -ErrorAction SilentlyContinue
        if (-not $mutexProcess.WaitForExit(5000)) {
            Stop-Process -Id $mutexProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path $runKeyPath) {
        if ($hadOriginalRunValue) {
            Set-ItemProperty -Path $runKeyPath -Name $runValueName -Value $originalRunValue
        } else {
            Remove-ItemProperty -Path $runKeyPath -Name $runValueName -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path $installDir) {
        $leftoverUninstaller = Join-Path $installDir 'unins000.exe'
        if (Test-Path $leftoverUninstaller) {
            try {
                Invoke-BoundedProcess -FilePath $leftoverUninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -TimeoutMs 60000 | Out-Null
            } catch {
            }
        }
    }
    Remove-Item -Path $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
