param(
    [string]$PythonExe = $env:PYTHON_EXE,
    [switch]$Offline,
    [string]$Wheelhouse = (Join-Path $PSScriptRoot 'wheelhouse'),
    [switch]$WithUI,
    [switch]$WithLocust,
    [switch]$WithVision
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Install 64-bit Python 3.11+ first, or pass -PythonExe <python.exe>.' }
    $PythonExe = $pythonCommand.Source
}
function Invoke-CheckedPython([string]$Interpreter, [string[]]$PythonArgs) {
    & $Interpreter @PythonArgs
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE). Installation stopped; existing data was preserved." }
}
Invoke-CheckedPython $PythonExe @('-c', "import sys,struct; assert sys.version_info >= (3,11), 'Python 3.11+ is required'; assert struct.calcsize('P')==8, '64-bit Python is required'; print(sys.version)")
if ($Offline -and -not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) {
    throw 'Offline wheelhouse directory not found. Prepare wheels for the target Python version and Windows architecture.'
}
$venvDirectory = Join-Path $PSScriptRoot '.venv'
$servicePython = Join-Path $venvDirectory 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $servicePython -PathType Leaf)) {
    Invoke-CheckedPython $PythonExe @('-m', 'venv', $venvDirectory)
}
$requirements = @((Join-Path $PSScriptRoot 'requirements-core.txt'))
if ($WithUI) { $requirements += Join-Path $PSScriptRoot 'requirements-ui.txt' }
if ($WithLocust) { $requirements += Join-Path $PSScriptRoot 'requirements-locust.txt' }
if ($WithVision) { $requirements += Join-Path $PSScriptRoot 'requirements-vision.txt' }
$pipArguments = @('-m','pip','install','--disable-pip-version-check','--no-input')
if ($Offline) { $pipArguments += @('--no-index','--find-links',$Wheelhouse) }
foreach ($file in $requirements) { $pipArguments += @('-r',$file) }
Invoke-CheckedPython $servicePython $pipArguments
Invoke-CheckedPython $servicePython @('-c',"import fastapi,uvicorn,pydantic,pytest; print('Core dependencies ready')")
Write-Host 'Installation complete. Run .\start.ps1 (or start.bat), then open http://127.0.0.1:8000 .'
Write-Host 'New installations start with empty configuration. Use the web UI to register local resources and import definitions.'
Write-Host 'Existing databases, logs, reports, and project files were not changed by this installer.'
