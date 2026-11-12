param([switch]$CreateShortcut, [switch]$NoLaunch, [string]$StateReport)
$ErrorActionPreference = 'Stop'
# Ask the existing desktop shell to own the app. Start-Process/Popen from a
# terminal owned by another app can inherit that app's kill-on-close job.
$rvHostRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$rvExecutable = Join-Path $rvHostRoot 'build/dist/RemonteVoicePair/RemonteVoice.exe'
if (!(Test-Path -LiteralPath $rvExecutable -PathType Leaf)) {
    throw 'The local RemonteVoice executable has not been built/installed.'
}
$rvWorking = Split-Path -Parent $rvExecutable
if ($CreateShortcut) {
    $rvWsh = New-Object -ComObject WScript.Shell
    $rvDesktop = [string]$rvWsh.SpecialFolders.Item('Desktop')
    $rvShortcutPath = Join-Path $rvDesktop 'RemonteVoice.lnk'
    if (Test-Path -LiteralPath $rvShortcutPath) {
        $rvExisting = $rvWsh.CreateShortcut($rvShortcutPath)
        if (!$rvExisting.TargetPath.StartsWith($rvHostRoot + [IO.Path]::DirectorySeparatorChar,
                                               [StringComparison]::OrdinalIgnoreCase)) {
            throw 'A different RemonteVoice shortcut exists; it was preserved.'
        }
    }
    $rvShortcut = $rvWsh.CreateShortcut($rvShortcutPath)
    $rvShortcut.TargetPath = $rvExecutable
    $rvShortcut.Arguments = '--autostart'
    $rvShortcut.WorkingDirectory = $rvWorking
    $rvShortcut.IconLocation = $rvExecutable + ',0'
    $rvShortcut.Description = 'RemonteVoice — local phone audio and laptop host'
    $rvShortcut.Save()
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($rvWsh) | Out-Null
}
if (!$NoLaunch) {
    $rvShell = New-Object -ComObject Shell.Application
    # A newly created Shell.Application can execute in this launcher process.
    # Obtain the desktop's automation object instead: its Application lives
    # in Explorer, so Explorer creates the Host outside this launcher's job.
    $rvDesktopView = $rvShell.Windows().Item([uint32]8).Document
    if ($null -eq $rvDesktopView) { throw 'Windows desktop shell is not available.' }
    $rvDesktopApplication = $rvDesktopView.Application
    # Interactive application, normal window. No administrator verb, task,
    # background service, login startup, or firewall change is created.
    $rvDesktopApplication.ShellExecute($rvExecutable, '--autostart', $rvWorking, 'open', 1)
    if ($StateReport) {
        $rvStateScript = Join-Path $rvHostRoot 'Desktop-State.ps1'
        $rvPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        $rvArguments = '-NoProfile -File "' + $rvStateScript + '" -ReportPath "' + [IO.Path]::GetFullPath($StateReport) + '"'
        $rvDesktopApplication.ShellExecute($rvPowerShell, $rvArguments, $rvWorking, 'open', 0)
    }
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($rvShell) | Out-Null
}
