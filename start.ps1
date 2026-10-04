param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000
)
$py = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "F:\Anaconda3\python.exe" }
Set-Location -Location $PSScriptRoot
& $py "backend\run.py" --host $Host --port $Port --no-browser