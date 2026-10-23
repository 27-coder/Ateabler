$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "ateable.ps1") serve @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
