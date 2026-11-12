# Prepared local test only. Run/Cleanup require separately approved elevation.
[CmdletBinding()]
param(
    [ValidateSet('Preflight','Run','Cleanup')][string]$Action='Preflight',
    [Parameter(Mandatory=$true)][string]$ExpectedPlanHash
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
# A caller running PowerShell 7 can pass an incompatible PSModulePath to 5.1.
# Load only the modules bundled with the executing Windows PowerShell runtime.
foreach($module in @('Microsoft.PowerShell.Utility','Microsoft.PowerShell.Management','Microsoft.PowerShell.Security','CimCmdlets')){
    Import-Module (Join-Path $PSHOME ('Modules\'+$module+'\'+$module+'.psd1')) -ErrorAction Stop
}
$planPath=Join-Path $PSScriptRoot 'plan.json'
if($ExpectedPlanHash -cnotmatch '^[0-9a-f]{64}$'){throw 'Invalid expected plan hash'}
if((Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash.ToLower() -cne $ExpectedPlanHash){throw 'Plan changed'}
$plan=Get-Content -LiteralPath $planPath -Raw -Encoding UTF8|ConvertFrom-Json
if($plan.schema_version -ne 1 -or $plan.label -cnotmatch '^[0-9]{3}$' -or
   $plan.driver_install -ne $false -or $plan.microphone_enable -ne $false -or
   $plan.audio_route_changes -ne $false -or $plan.soft_stop_seconds -ne 60 -or $plan.hard_exit_seconds -ne 90){throw 'Unsupported plan'}
if(@($plan.broker_args).Count -ne 1 -or $plan.broker_args[0] -cne '--lab-muted-only' -or
   @($plan.client_args).Count -ne 1 -or $plan.client_args[0] -cne '--broker-lab-probe'){throw 'Unsupported commands'}
[void][guid]::Parse($plan.operation_id)
if((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower() -cne $plan.script_sha256){throw 'Worker changed'}
$programFiles=[Environment]::GetFolderPath('ProgramFiles')
$target=[IO.Path]::GetFullPath((Join-Path $programFiles ('RemonteVoice-AdmissionLab-'+$plan.label)))
if($plan.target -cne $target -or [IO.Path]::GetDirectoryName($target) -cne $programFiles){throw 'Unexpected target'}
$source=[IO.Path]::GetFullPath($plan.source)
$package=Join-Path $target 'package'
$marker=Join-Path $target 'owner.json'
$receiptPath=Join-Path $PSScriptRoot 'reports\run.json'
$entries=@($plan.files.PSObject.Properties)
if($entries.Count -lt 5 -or $entries.Count -gt 4097){throw 'Invalid file inventory'}

function Assert-PlainPath([string]$Path) {
    $current=Get-Item -LiteralPath $Path -Force
    while($null -ne $current){
        if(($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Reparse path rejected'}
        if($current -is [IO.FileInfo]){$current=$current.Directory}else{$current=$current.Parent}
    }
}
function Get-PlainTree([string]$Root) {
    Assert-PlainPath $Root
    $pending=[Collections.Generic.Stack[string]]::new();$pending.Push($Root)
    while($pending.Count -gt 0){
        $directory=$pending.Pop()
        foreach($item in @(Get-ChildItem -LiteralPath $directory -Force)){
            if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Reparse tree rejected'}
            if($item.PSIsContainer){$pending.Push($item.FullName)}
            $item
        }
    }
}
function Get-Leaf([string]$Root,[string]$Relative) {
    if($Relative -match '[\\:\x00-\x1f<>"|?*]' -or $Relative.StartsWith('/')){throw 'Invalid relative file'}
    foreach($part in $Relative.Split('/')){
        if($part -eq '' -or $part -in @('.','..') -or $part -match '[. ]$'){throw 'Invalid file component'}
    }
    $leaf=[IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if(-not $leaf.StartsWith($Root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'File escaped package'}
    return $leaf
}
function Assert-Package([string]$Root) {
    $tree=@(Get-PlainTree $Root)
    $files=@($tree|Where-Object{-not $_.PSIsContainer})
    if($files.Count -ne $entries.Count){throw 'Package file count changed'}
    $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach($entry in $entries){
        $leaf=Get-Leaf $Root $entry.Name
        if(-not $seen.Add($leaf) -or $entry.Value -cnotmatch '^[0-9a-f]{64}$'){throw 'Invalid inventory entry'}
        $item=Get-Item -LiteralPath $leaf -Force
        if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Invalid package file'}
        if((Get-FileHash -LiteralPath $leaf -Algorithm SHA256).Hash.ToLower() -cne $entry.Value){throw 'Package hash mismatch'}
    }
}
function Assert-Administrator {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{
        $principal=[Security.Principal.WindowsPrincipal]::new($identity)
        if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Approved elevated worker required'}
    }finally{$identity.Dispose()}
}
function Get-OwnedProcesses {
    $result=@()
    foreach($process in @(Get-CimInstance Win32_Process -Filter "Name='RemonteVoice.exe' OR Name='RemonteVoiceBroker.exe'")){
        if([string]::IsNullOrEmpty($process.ExecutablePath)){throw 'Process image ownership unavailable'}
        if($process.ExecutablePath.StartsWith($target+'\',[StringComparison]::OrdinalIgnoreCase)){$result += $process}
    }
    return $result
}
function Remove-OwnedTarget {
    if(-not (Test-Path -LiteralPath $target)){return}
    # Recheck absolute containment and the whole tree immediately before deletion.
    if([IO.Path]::GetFullPath($target) -cne $plan.target -or
       [IO.Path]::GetDirectoryName($target) -cne $programFiles){throw 'Cleanup target changed'}
    [void]@(Get-PlainTree $target)
    $owner=Get-Content -LiteralPath $marker -Raw -Encoding UTF8|ConvertFrom-Json
    if($owner.operation_id -cne $plan.operation_id -or $owner.plan_sha256 -cne $ExpectedPlanHash){throw 'Cleanup owner mismatch'}
    if(@(Get-OwnedProcesses).Count -ne 0){throw 'Owned process still running; directory retained'}
    Remove-Item -LiteralPath $target -Recurse -Force
    if(Test-Path -LiteralPath $target){throw 'Cleanup incomplete'}
}
function Save-Receipt {
    $report.updated_at=[DateTime]::UtcNow.ToString('o')
    [IO.File]::WriteAllText($receiptPath,($report|ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))
}

Assert-PlainPath $PSScriptRoot
Assert-PlainPath $programFiles
if($Action -eq 'Cleanup'){
    Assert-Administrator
    Remove-OwnedTarget
    Write-Output 'Exact owned test directory removed or already absent'
    exit 0
}
Assert-Package $source
if(Test-Path -LiteralPath $target){throw 'Target exists; inspect prior outcome instead of repeating Run'}
if($Action -eq 'Preflight'){
    [pscustomobject]@{success=$true;prepared_only=$true;files=$entries.Count;target=$target;plan_sha256=$ExpectedPlanHash}|ConvertTo-Json
    exit 0
}
Assert-Administrator
if(Test-Path -LiteralPath $receiptPath){throw 'Run receipt exists; inspect prior outcome before another attempt'}
$report=[ordered]@{operation_id=$plan.operation_id;plan_sha256=$ExpectedPlanHash;status='staging';
    cleanup_confirmed=$false;broker_pid=$null;broker_exit=$null;error=$null;updated_at=$null}
$created=$false;$broker=$null
try{
    Save-Receipt
    [void](New-Item -ItemType Directory -Path $target -ErrorAction Stop);$created=$true
    $administrators=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl=[Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($administrators);$acl.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544','S-1-5-32-545')){
        $rights=if($sid -eq 'S-1-5-32-545'){'ReadAndExecute'}else{'FullControl'}
        $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),
            $rights,'ContainerInherit,ObjectInherit','None','Allow')
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $target -AclObject $acl
    [IO.File]::WriteAllText($marker,(@{operation_id=$plan.operation_id;plan_sha256=$ExpectedPlanHash}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    [void](New-Item -ItemType Directory -Path $package)
    foreach($entry in $entries){
        $leaf=Get-Leaf $package $entry.Name
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($leaf))
        Copy-Item -LiteralPath (Get-Leaf $source $entry.Name) -Destination $leaf
    }
    # Trusted ownership applies to every copied file, directory and the marker.
    foreach($item in @(Get-PlainTree $target)){
        $childAcl=Get-Acl -LiteralPath $item.FullName
        $childAcl.SetOwner($administrators)
        Set-Acl -LiteralPath $item.FullName -AclObject $childAcl
    }
    Assert-Package $package
    $broker=Start-Process -FilePath (Join-Path $package 'RemonteVoiceBroker.exe') -ArgumentList '--lab-muted-only' -WorkingDirectory $package -WindowStyle Hidden -PassThru
    [void]$broker.Handle
    $report.broker_pid=$broker.Id;$report.status='broker_started';Save-Receipt
    # The model runs the limited, muted client separately while this worker waits.
    if(-not $broker.WaitForExit(110000)){
        $broker.Kill();[void]$broker.WaitForExit(5000)
        throw 'Broker exceeded external deadline'
    }
    $report.broker_exit=$broker.ExitCode
    if($broker.ExitCode -ne 0){throw 'Broker did not exit cleanly'}
    $report.status='broker_finished'
}catch{
    $report.status='failed';$report.error=$_.Exception.Message
}finally{
    if($null -ne $broker){$broker.Dispose()}
    try{
        if($created){Remove-OwnedTarget}
        $report.cleanup_confirmed=(-not (Test-Path -LiteralPath $target))
    }catch{
        $report.cleanup_confirmed=$false
        $report.error='Cleanup not confirmed; retain receipt and exact test folder for inspection'
    }
    Save-Receipt
}
if($report.status -ne 'broker_finished' -or -not $report.cleanup_confirmed){exit 1}
exit 0
