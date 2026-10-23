$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonCommand = if ($env:ATEABLE_PYTHON) { $env:ATEABLE_PYTHON } else { "python" }
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$InstallMarker = Join-Path $VenvDir ".ateable-desktop-installed"

Set-Location $ProjectDir

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "Python 3.11 or newer is required."
}
& $PythonCommand -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required."
}
if (-not (Test-Path $VenvPython)) {
    & $PythonCommand -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Python virtual environment."
    }
}

$Pyproject = Get-Item (Join-Path $ProjectDir "backend\pyproject.toml")
$NeedsInstall = -not (Test-Path $InstallMarker)
if (-not $NeedsInstall) {
    $NeedsInstall = $Pyproject.LastWriteTimeUtc -gt (Get-Item $InstallMarker).LastWriteTimeUtc
}
if ($NeedsInstall) {
    & $VenvPython -m ensurepip --upgrade | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not initialize pip in the Python virtual environment."
    }
    & $VenvPython -m pip install --disable-pip-version-check --quiet -e "backend[desktop]"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install Ateable's desktop dependencies."
    }
    New-Item -ItemType File -Force -Path $InstallMarker | Out-Null
}

New-Item -ItemType Directory -Force -Path data | Out-Null
if (-not $env:ATEABLE_STATE_DIR) {
    $env:ATEABLE_STATE_DIR = Join-Path $ProjectDir "data"
}
if (-not $env:ATEABLE_DATABASE_PATH) {
    $env:ATEABLE_DATABASE_PATH = Join-Path $ProjectDir "data\ateable.db"
}
if (-not $env:ATEABLE_OUTPUT_DIR) {
    $env:ATEABLE_OUTPUT_DIR = Join-Path $ProjectDir "downloads"
}
if (-not $env:ATEABLE_CONFIG_DIR) {
    $env:ATEABLE_CONFIG_DIR = Join-Path $ProjectDir "data\config"
}
if (-not $env:ATEABLE_NETWORK_MODE) {
    $env:ATEABLE_NETWORK_MODE = "tor"
}
if (-not $env:ATEABLE_TOR_PROXY) {
    $env:ATEABLE_TOR_PROXY = "socks5h://127.0.0.1:9050"
}
if (-not $env:ATEABLE_ADMIN_KEY) {
    $LocalAdminKey = & $VenvPython -c `
        "from ateable.device_config import DeviceConfig; print(DeviceConfig.load().admin_key or '')"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read Ateable's local device configuration."
    }
    if ($LocalAdminKey) {
        $env:ATEABLE_ADMIN_KEY = $LocalAdminKey.Trim()
    }
}
if (-not $env:ATEABLE_API_URL) {
    $LocalApiUrl = & $VenvPython -c `
        "from ateable.device_config import DeviceConfig; print(DeviceConfig.load().controller_url)"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read Ateable's local controller URL."
    }
    $env:ATEABLE_API_URL = $LocalApiUrl.Trim()
}

& $VenvPython -m ateable.desktop @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
