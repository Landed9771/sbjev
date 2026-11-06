<#
.SYNOPSIS
  Undoes install.ps1: stops the server, removes start-at-login and the sbjev:// link.
  Leaves this folder (config.json, cache/) alone; delete it yourself if you want it gone.
#>
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# the supervisor first, so it doesn't restart the server we stop next
Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape((Join-Path $here 'start-server.vbs')) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'sbjev\.py --serve|audio_snap\.py --worker' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'sbjev server.lnk') -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Classes\sbjev' -Recurse -ErrorAction SilentlyContinue
Write-Host "sbjev stopped and unregistered. Remove the userscript from Tampermonkey/Violentmonkey yourself."
