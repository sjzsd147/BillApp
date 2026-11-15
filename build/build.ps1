# Build BillApp Electron Desktop Application (Frontend + Python Sidecar + Electron)
param(
    [string]$OutDir = "",
    [switch]$DirOnly
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildScript = Join-Path $PSScriptRoot "build.mjs"

$NodeArgs = @($BuildScript)
if ($DirOnly) {
    $NodeArgs += "--dir"
}
else {
    $NodeArgs += "--win"
}
if ($OutDir) {
    $NodeArgs += @("--out", $OutDir)
}

& node @NodeArgs
if ($LASTEXITCODE -ne 0) {
    throw "Desktop build failed with exit code $LASTEXITCODE"
}
