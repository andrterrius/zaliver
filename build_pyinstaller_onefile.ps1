#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
pyinstaller --noconfirm zaliver_onefile.spec
Write-Host ""
Write-Host "Output: $PSScriptRoot\dist\zaliver.exe"
