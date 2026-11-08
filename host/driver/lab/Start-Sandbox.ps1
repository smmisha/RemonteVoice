# Launch the installed packaged GUI through the desktop shell.
# No driver installation, host protection changes, or microphone access.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$cli=Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wsb.exe'
$app=Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\WindowsSandboxRemoteSession.exe'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$records=Join-Path $project '.harness\evidence\sandbox-launcher'
$mutex=[Threading.Mutex]::new($false,'Local\RemoteVoice-Sandbox-Launcher')
$locked=$false
$launchId=[guid]::NewGuid().ToString()
$recordPath=$null
$errorMessage=$null
$record=[ordered]@{launch_id=$launchId;status='preflight';memory_mb=2048;
    mode='packaged_gui_owns_guest';persistent_controller=$false;settings_changed=$false}

function Save-Record {
    if($null -ne $recordPath){
        $record.updated_at=[DateTime]::UtcNow.ToString('o')
        [IO.File]::WriteAllText($recordPath,($record|ConvertTo-Json -Depth 3),[Text.UTF8Encoding]::new($false))
    }
}
function Get-SandboxIds {
    $process=[Diagnostics.Process]::new()
    try {
        $process.StartInfo=[Diagnostics.ProcessStartInfo]::new()
        $process.StartInfo.FileName=$cli
        $process.StartInfo.Arguments='list --raw'
        $process.StartInfo.UseShellExecute=$false
        $process.StartInfo.CreateNoWindow=$true
        $process.StartInfo.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
        $process.StartInfo.RedirectStandardOutput=$true
        $process.StartInfo.RedirectStandardError=$true
        if(-not $process.Start()){throw 'Sandbox CLI did not start'}
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        if(-not $process.WaitForExit(15000)){
            $process.Kill();[void]$process.WaitForExit(3000)
            throw 'Sandbox CLI timed out'
        }
        if($process.ExitCode -ne 0){throw ('Sandbox CLI exit '+$process.ExitCode)}
        if(-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)){throw 'Sandbox CLI output incomplete'}
        $result=$stdout.Result|ConvertFrom-Json
        return @($result.WindowsSandboxEnvironments|ForEach-Object{$_.Id})
    } finally {$process.Dispose()}
}
try {
    try {$locked=$mutex.WaitOne(0)} catch [Threading.AbandonedMutexException] {$locked=$true}
    if(-not $locked){return}
    if(-not (Test-Path -LiteralPath $cli) -or -not (Test-Path -LiteralPath $app)){
        throw 'The Windows Sandbox app and its execution aliases are required.'
    }
    [void][IO.Directory]::CreateDirectory($records)
    $recordPath=Join-Path $records ($launchId+'.json')
    $existing=@(Get-SandboxIds)
    if($existing.Count){
        $record.status='already_running';Save-Record
        return
    }
    $memory=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    if($memory.AvailableMBytes -lt 2560 -or ($memory.CommitLimit-$memory.CommittedBytes)/1MB -lt 4096){
        throw 'At least 2.5 GB free RAM and 4 GB commit headroom are needed. Close unused applications first.'
    }
    $record.status='start_requested';Save-Record
    $config='<Configuration><MemoryInMB>2048</MemoryInMB><vGPU>Disable</vGPU><Networking>Disable</Networking><AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput><ClipboardRedirection>Disable</ClipboardRedirection><PrinterRedirection>Disable</PrinterRedirection></Configuration>'
    $requestedAt=[DateTime]::Now
    $shell=New-Object -ComObject Shell.Application
    try {
        # Explorer owns the GUI, outside the terminal/Codex job. The packaged
        # alias preserves package identity and dependency resolution. --wsb
        # takes XML, not a file path. The GUI creates and closes its own guest;
        # wsb start + connect would instead leave lifecycle ownership here.
        $desktop=$shell.Windows().Item([uint32]8).Document.Application
        if($null -eq $desktop){throw 'Windows desktop shell is not available'}
        $desktop.ShellExecute($app,('--wsb "'+$config+'"'),$env:SystemRoot,'open',1)
    } finally {[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)}
    $guests=@()
    for($attempt=0;$attempt -lt 20;$attempt++){
        $guests=@(Get-SandboxIds)
        if($guests.Count){break}
        Start-Sleep -Milliseconds 500
    }
    if($guests.Count -ne 1){throw 'Sandbox creation was not confirmed; inspect its window before retrying'}
    $record.sandbox_id=$guests[0]
    $candidate=@(Get-CimInstance Win32_Process -Filter "Name='WindowsSandboxRemoteSession.exe'" |
        Where-Object{$_.CreationDate -ge $requestedAt -and $_.CommandLine -like '*--wsb *'})
    if($candidate.Count -ne 1){throw 'Sandbox window identity was not confirmed'}
    $record.window_pid=$candidate[0].ProcessId
    $record.window_parent_pid=$candidate[0].ParentProcessId
    $record.status='guest_created';Save-Record
} catch {
    $record.status='failed';$record.error=$_.Exception.Message;Save-Record
    $errorMessage=$_.Exception.Message
} finally {
    if($locked){$mutex.ReleaseMutex()}
    $mutex.Dispose()
}
if($null -ne $errorMessage){
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($errorMessage,'Windows Sandbox')
}
