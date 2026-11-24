$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$clientExecutable = Join-Path $root ('' + [char]0x5c0f + [char]0x706b + [char]0x7bad + '-' + [char]0x53d1 + [char]0x9001 + [char]0x7aef + '.exe')
$feed = Join-Path $root 'update-feed'
$stopFile = Join-Path $root '.update-server-stop'
New-Item -ItemType Directory -Path $feed -Force | Out-Null
$executable = $null
# Run an immutable release asset, never the client executable being updated.
$manifestFile = Join-Path $feed 'latest.json'
if (Test-Path -LiteralPath $manifestFile) {
    $manifest = [IO.File]::ReadAllText($manifestFile) | ConvertFrom-Json
    $assetName = $manifest.artifacts.sender.file
    if ($assetName -notmatch '^sender-\d+\.\d+\.\d+-[a-f0-9]{12}\.exe$') { throw 'Invalid server asset in latest.json' }
    $asset = Join-Path $feed $assetName
    if (Test-Path -LiteralPath $asset) { $executable = $asset }
}
if (-not $executable -and (Test-Path -LiteralPath $clientExecutable)) {
    $runtime = Join-Path $root '.update-server-runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($clientExecutable)
    try { $digest = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hasher.Dispose() }
    $executable = Join-Path $runtime ('server-' + $digest + '.exe')
    if (-not (Test-Path -LiteralPath $executable)) { Copy-Item -LiteralPath $clientExecutable -Destination $executable }
}
$probe = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:18765/latest.json')
$probe.Proxy = $null
$probe.Timeout = 2000
$probeResponse = $null
try {
    $probeResponse = $probe.GetResponse()
    $reader = New-Object System.IO.StreamReader($probeResponse.GetResponseStream())
    $existing = $reader.ReadToEnd() | ConvertFrom-Json
    if ($existing.version -and $existing.artifacts.sender -and $existing.artifacts.receiver) {
        Write-Host 'Update server is already running on port 18765. No duplicate was started.'
        exit 0
    }
} catch { } finally { if ($probeResponse) { $probeResponse.Dispose() } }
Write-Host 'XiaoRocket update server: http://192.168.77.3:18765/latest.json'
Write-Host 'Runs in the background. Keep this PC on. Use stop_update_server.cmd to stop.'
if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile -Force }
if ($executable) {
    $process = Start-Process -FilePath $executable -ArgumentList @('--serve-updates', ('"' + $feed + '"'), '--stop-file', ('"' + $stopFile + '"')) -WindowStyle Hidden -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) { throw 'Server exited. Check update-feed/server-error.log and port 18765.' }
        $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:18765/latest.json')
        $request.Proxy = $null
        $request.Timeout = 1000
        try {
            $response = $request.GetResponse()
            $response.Dispose()
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) { throw 'Update service is not ready. Check update-feed/latest.json and port 18765.' }
    Write-Host ('Update server started. Launcher process: ' + $process.Id)
} else {
    & python (Join-Path $root 'update_server.py') --serve-updates $feed --stop-file $stopFile
}
