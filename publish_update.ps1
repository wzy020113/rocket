param(
    [string]$Version,
    [string]$Notes = '',
    [string]$Source = 'http://192.168.77.3:18765'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$shownText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'app_version.py'))
$shownVersion = 'unknown'
if ($shownText -match "VERSION = '([^']+)'") { $shownVersion = $Matches[1] }
if (-not $Version) { $Version = Read-Host ('Current version is ' + $shownVersion + '. New version (for example ' + $shownVersion + ' -> next)') }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must be x.y.z' }
if ($Source -notmatch '^https?://[a-zA-Z0-9.\-:\[\]]+/?$') { throw 'Invalid update server URL' }
$utf8 = New-Object System.Text.UTF8Encoding($false)
$versionFile = Join-Path $PSScriptRoot 'app_version.py'
$oldVersion = [IO.File]::ReadAllText($versionFile)
$currentVersion = (& python -c "from app_version import VERSION;print(VERSION)")
if ($LASTEXITCODE -ne 0) { throw 'Python runtime is required on publisher PC' }
if (([version]$Version) -le ([version]$currentVersion)) { throw 'New version must be greater than current version' }
try {
    [IO.File]::WriteAllText($versionFile, "VERSION = '$Version'`nDEFAULT_UPDATE_SOURCE = '$Source'`n", $utf8)
    & python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed, release was not published' }
    $builds = Join-Path $PSScriptRoot ('release-builds\' + $Version)
    $prefix = [string][char]0x5c0f + [char]0x706b + [char]0x7bad + '-'
    $specs = @(
        (Join-Path $PSScriptRoot ($prefix + [char]0x53d1 + [char]0x9001 + [char]0x7aef + '.spec')),
        (Join-Path $PSScriptRoot ($prefix + [char]0x63a5 + [char]0x6536 + [char]0x7aef + '.spec'))
    )
    foreach ($spec in $specs) {
        if (-not (Test-Path -LiteralPath $spec)) { throw ('Missing spec: ' + $spec) }
        & python -m PyInstaller --noconfirm --distpath $builds --workpath (Join-Path $PSScriptRoot '.build-autoupdate') $spec
        if ($LASTEXITCODE -ne 0) { throw 'Packaging failed, release was not published' }
    }
    $publishArgs = @('update_server.py', '--publish-update', $Version, '--builds', $builds, '--output', (Join-Path $PSScriptRoot 'update-feed'))
    if ($Notes) { $publishArgs += @('--notes', $Notes) }
    & python @publishArgs
    if ($LASTEXITCODE -ne 0) { throw 'Publishing failed' }
    Write-Host "Published $Version at $Source/latest.json"
} catch {
    [IO.File]::WriteAllText($versionFile, $oldVersion, $utf8)
    throw
}
