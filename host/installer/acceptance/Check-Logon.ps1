# Read-only check of the installed product after a sign-in or reboot.
# Proves that the logon task started the windowless broker and that the
# installation is the product one, without changing anything.
param(
    [string]$EvidenceDir = 'M:\Projectorium\RemonteVoice\host\build\package-acceptance'
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null
$root = 'C:\Program Files\RemonteVoice'
$brokerLog = Join-Path $env:APPDATA 'RemonteVoice\broker.log'
$logLines = @(Get-Content -LiteralPath $brokerLog -ErrorAction SilentlyContinue | ForEach-Object { [string]$_ })
$lastStart = $null
for ($index = $logLines.Count - 1; $index -ge 0; $index--) {
    if ($logLines[$index] -match ' start ') { $lastStart = [string]$logLines[$index]; break }
}
$broker = Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue
$taskCommand = $null
$taskUserId = $null
$taskState = $null
if (Test-Path 'C:\Windows\System32\Tasks\RemonteVoice') {
    $task = [xml](Get-Content 'C:\Windows\System32\Tasks\RemonteVoice' -Raw)
    $taskCommand = [string]$task.Task.Actions.Exec.Command
    $taskUserId = [string]$task.Task.Principals.Principal.UserId
    $taskState = [string]$task.Task.Settings.Enabled
}
$stateVersion = $null
$statePath = 'C:\ProgramData\RemonteVoice\install-state.json'
if (Test-Path $statePath) { $stateVersion = (Get-Content $statePath -Raw | ConvertFrom-Json).version }
$bootUtc = $null
try { $bootUtc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o') } catch { $bootUtc = $null }
$evidence = [ordered]@{
    operation = 'logon-check'
    captured_utc = [DateTime]::UtcNow.ToString('o')
    boot_utc = $bootUtc
    user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    administrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    product_directory_present = Test-Path $root
    product_files = @(Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue).Count
    task_command = $taskCommand
    task_user = $taskUserId
    task_enabled = $taskState
    task_starts_product_broker = ($taskCommand -eq (Join-Path $root 'RemonteVoiceBroker.exe'))
    product_installed = ((Test-Path $statePath) -and ($taskCommand -eq (Join-Path $root 'RemonteVoiceBroker.exe')))
    broker_pid = if ($broker) { $broker.Id } else { $null }
    broker_started = if ($broker) { $broker.StartTime.ToUniversalTime().ToString('o') } else { $null }
    broker_window_title = if ($broker) { $broker.MainWindowTitle } else { $null }
    broker_visible_window = if ($broker) { $broker.MainWindowHandle } else { $null }
    broker_wireless_console_absent = [bool]($broker -and $broker.MainWindowHandle -eq 0 -and -not $broker.MainWindowTitle)
    pipes = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice-Mic-' })
    broker_log_start_lines = @($logLines | Where-Object { $_ -match ' start ' } | ForEach-Object { [string]$_ })
    broker_log_last_start = $lastStart
    broker_log_failures = @($logLines | Where-Object { $_ -match ' failure ' })
    state_version = $stateVersion
    uninstall_version = (Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice' -ErrorAction SilentlyContinue).DisplayVersion
    app_running = [bool](Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    appdata_preserved = Test-Path (Join-Path $env:APPDATA 'RemonteVoice')
    dev_install_present = Test-Path 'C:\Program Files\RemonteVoice-Dev'
}
$evidence.ok = ($evidence.product_installed -and $evidence.product_directory_present -and $evidence.task_starts_product_broker -and
    $evidence.broker_wireless_console_absent -and $evidence.pipes.Count -eq 1 -and
    $evidence.broker_log_failures.Count -eq 0)
$target = Join-Path $EvidenceDir 'logon-check.json'
($evidence | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $target -Encoding utf8
Write-Host ($evidence | ConvertTo-Json -Depth 6)
Write-Host ('RESULT: {0}' -f $(if ($evidence.ok) { 'ok' } else { 'failed' }))
exit $(if ($evidence.ok) { 0 } else { 1 })
