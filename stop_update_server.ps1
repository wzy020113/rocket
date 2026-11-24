$ErrorActionPreference = 'Stop'
$stopFile = Join-Path $PSScriptRoot '.update-server-stop'
[IO.File]::WriteAllText($stopFile, 'stop')
Write-Host 'Update server stop requested. Active update downloads may be interrupted.'
Write-Host 'File transfers between sender and receiver are not affected.'
