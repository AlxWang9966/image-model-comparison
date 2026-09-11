param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$runtime = $null
$runtimeArgs = @()

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $runtime = $launcher.Source
        $runtimeArgs = @("-3")
    }
}

if (-not $runtime) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike "*\WindowsApps\*") {
        & $python.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $runtime = $python.Source
        }
    }
}

if (-not $runtime) {
    $az = Get-Command az -ErrorAction SilentlyContinue
    if ($az) {
        $azurePython = Join-Path (Split-Path (Split-Path $az.Source -Parent) -Parent) "python.exe"
        if (Test-Path -LiteralPath $azurePython -PathType Leaf) {
            & $azurePython -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $runtime = $azurePython
                Write-Host "Using the Python runtime bundled with Azure CLI."
            }
        }
    }
}

if (-not $runtime) {
    throw "Python 3.10+ is required. Install Python, then run this script again."
}

$appArgs = @("-m", "image_lab", "--port", "$Port")
if (-not $NoBrowser) {
    $appArgs += "--open"
}

Push-Location $PSScriptRoot
try {
    Write-Host "Starting Image Lab at http://127.0.0.1:$Port"
    Write-Host "Azure generation uses your existing az login. Press Ctrl+C to stop."
    & $runtime @runtimeArgs @appArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Image Lab exited with code $LASTEXITCODE. See the error above."
    }
} finally {
    Pop-Location
}
