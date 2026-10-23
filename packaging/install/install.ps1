param(
    [ValidateSet("admin", "node", "worker")][string]$Role = "admin",
    [string]$Prefix,
    [switch]$Gui,
    [switch]$Offline,
    [string]$Wheelhouse,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$PythonCommand = if ($env:ATEABLE_PYTHON) { $env:ATEABLE_PYTHON } else { "python" }
$InstallArgs = @((Join-Path $PSScriptRoot "install.py"), "--role", $Role)
if ($Prefix) { $InstallArgs += @("--prefix", $Prefix) }
if ($Gui) { $InstallArgs += "--gui" }
if ($Offline) { $InstallArgs += "--offline" }
if ($Wheelhouse) { $InstallArgs += @("--wheelhouse", $Wheelhouse) }
if ($DryRun) { $InstallArgs += "--dry-run" }
& $PythonCommand @InstallArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
