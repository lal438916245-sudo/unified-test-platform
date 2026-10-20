param(
    [Alias('Host')][string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$PythonExe = $env:PYTHON_EXE,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$PlatformArgs
)
$ErrorActionPreference = 'Stop'
$py = $PythonExe
if (-not $py) {
    $localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { $py = $localPython }
    else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if (-not $command) { throw 'Python not found. Use .\start.ps1 -PythonExe <python.exe>' }
        $py = $command.Source
    }
}
Set-Location -LiteralPath $PSScriptRoot
& $py "backend\run.py" --host $BindHost --port $Port --no-browser @PlatformArgs
exit $LASTEXITCODE
