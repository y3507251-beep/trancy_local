param(
    [string]$RuntimeRoot = 'D:\dev\TrancyKokoro',
    [string]$PythonPath = 'D:\dev\Python\Python313\python.exe'
)
$ErrorActionPreference = 'Stop'
$runtimePath = [System.IO.Path]::GetFullPath($RuntimeRoot)
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Specify an installed Python 3.13 with -PythonPath.' }
    $PythonPath = $pythonCommand.Source
}
$pythonVersion = & $PythonPath -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.13') {
    throw 'Kokoro setup requires Python 3.13. Use -PythonPath to select it.'
}
[System.IO.Directory]::CreateDirectory($runtimePath) | Out-Null
$venvPath = Join-Path $runtimePath '.venv'
$runtimePython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    & $PythonPath -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the isolated Kokoro environment.' }
}
& $runtimePython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-kokoro.txt')
if ($LASTEXITCODE -ne 0) { throw 'Kokoro package installation failed.' }
$modelPath = Join-Path $runtimePath 'models'
[System.IO.Directory]::CreateDirectory($modelPath) | Out-Null
$releaseUrl = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/'
$assets = @(
    @{ Name = 'kokoro-v1.0.int8.onnx'; Size = 92361271; Hash = '6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb' },
    @{ Name = 'voices-v1.0.bin'; Size = 28214398; Hash = 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d' }
)
foreach ($asset in $assets) {
    $destination = Join-Path $modelPath $asset.Name
    $valid = (Test-Path -LiteralPath $destination -PathType Leaf) -and
        (Get-Item -LiteralPath $destination).Length -eq $asset.Size -and
        (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $asset.Hash
    if ($valid) {
        Write-Output "Verified existing $($asset.Name)"
        continue
    }
    $downloadPath = Join-Path $modelPath ('download-' + [Guid]::NewGuid().ToString('N') + '-' + $asset.Name)
    Write-Output "Downloading $($asset.Name) once..."
    Invoke-WebRequest -Uri ($releaseUrl + $asset.Name) -OutFile $downloadPath -UseBasicParsing
    if ((Get-Item -LiteralPath $downloadPath).Length -ne $asset.Size -or
        (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash -ne $asset.Hash) {
        throw "Download check failed. The file was retained at $downloadPath"
    }
    # Retain any prior invalid file rather than deleting or overwriting it.
    if (Test-Path -LiteralPath $destination) {
        $retainedPath = $destination + '.invalid-' + [Guid]::NewGuid().ToString('N')
        Move-Item -LiteralPath $destination -Destination $retainedPath
    }
    Move-Item -LiteralPath $downloadPath -Destination $destination
}
@{ owner = 'trancy-offline-kokoro'; engine = 'Kokoro-82M v1.0 int8'; root = $runtimePath } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimePath 'trancy-kokoro.json') -Encoding UTF8
Write-Output "Offline Kokoro installed at $runtimePath"
if ($runtimePath -ne 'D:\dev\TrancyKokoro') {
    Write-Output "Set TRANCY_KOKORO_ROOT to $runtimePath before launching the desktop app."
}
