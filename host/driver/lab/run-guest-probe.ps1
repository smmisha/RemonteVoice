# Run only a hash-verified synthetic test in the exact approved guest.
param([Parameter(Mandatory=$true)][ValidateSet('pcm_probe','loopback_probe','acl_probe')][string]$Name,
      [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedHash)
$ErrorActionPreference='Stop'
$rvDir='C:\Windows\Temp\RemonteVoiceLab-20261025'
$rvReceipt=[ordered]@{success=$false;probe=$Name;binary_sha256=$ExpectedHash.ToLowerInvariant()}
try{
    $rvConfig=Get-Content "$rvDir\lab-config.json" -Raw|ConvertFrom-Json
    $rvSerial=(Get-CimInstance Win32_BIOS).SerialNumber -replace '^VMware-','' -replace '[^0-9a-fA-F]',''
    $rvExpected=$rvConfig.vm_bios_uuid -replace '^VMware-','' -replace '[^0-9a-fA-F]',''
    if((Get-CimInstance Win32_ComputerSystem).Manufacturer -notlike 'VMware*' -or $rvSerial.Length -ne 32 -or $rvSerial -ne $rvExpected -or (Get-CimInstance Win32_OperatingSystem).BuildNumber -ne [string]$rvConfig.guest_build){throw 'Wrong target VM'}
    if((Get-CimInstance Win32_SystemDriver -Filter "Name='RemonteVoiceAudio'").State -ne 'Running'){throw 'Development driver not running'}
    $rvSource="\\vmware-host\Shared Folders\RemonteVoiceLab\$Name.exe"
    if((Get-FileHash -LiteralPath $rvSource).Hash -ne $ExpectedHash){throw 'Source probe hash mismatch'}
    $rvBinary="$rvDir\$Name-verified.exe"
    Copy-Item -LiteralPath $rvSource -Destination $rvBinary -Force
    if((Get-FileHash -LiteralPath $rvBinary).Hash -ne $ExpectedHash){throw 'Guest probe hash mismatch'}
    $rvReport="$rvDir\$($Name -replace '_','-').txt"
    if(Test-Path -LiteralPath $rvReport){Remove-Item -LiteralPath $rvReport -Force}
    $rvProcess=Start-Process -FilePath $rvBinary -WindowStyle Hidden -PassThru
    $rvReceipt.pid=$rvProcess.Id
    if(-not $rvProcess.WaitForExit(30000)){
        $rvReceipt.timed_out=$true
        $rvProcess.Kill()
        $rvReceipt.cleanup_confirmed=$rvProcess.WaitForExit(5000)
        throw 'Synthetic test exceeded deadline'
    }
    $rvProcess.Refresh()
    $rvReceipt.exit_code=$rvProcess.ExitCode
    $rvReceipt.success=($rvProcess.ExitCode -eq 0)
    if(Test-Path -LiteralPath $rvReport){$rvReceipt.report_sha256=(Get-FileHash $rvReport).Hash.ToLowerInvariant()}
}catch{$rvReceipt.error=$_.Exception.Message}
$rvReceipt.finished_utc=[DateTime]::UtcNow.ToString('o')
$rvReceipt|ConvertTo-Json -Depth 4|Set-Content "$rvDir\$Name-receipt.json" -Encoding utf8
if(-not $rvReceipt.success){exit 1}
