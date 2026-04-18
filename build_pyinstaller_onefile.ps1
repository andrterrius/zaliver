#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
pyinstaller --noconfirm zaliver_onefile_windows.spec
Write-Host ""
Write-Host "Output: $PSScriptRoot\dist\Zaliver.exe"
