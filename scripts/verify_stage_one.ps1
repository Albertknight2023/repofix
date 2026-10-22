param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
# The script directory lives under the project root.
$projectRoot = Split-Path -Parent $PSScriptRoot
$taskRoot = Join-Path $projectRoot "benchmarks\tasks\pagination-zero-page-size"
$publicTests = Join-Path $taskRoot "repo\tests"
$graderTests = Join-Path $taskRoot "grader_tests"
$initialSource = Join-Path $taskRoot "repo\src"
$referenceSource = Join-Path $taskRoot "reference\src"
$previousPythonPath = $env:PYTHONPATH

try {
    Write-Host "[1/2] Verifying that the initial implementation fails..."
    $env:PYTHONPATH = $initialSource
    & $Python -m pytest $publicTests $graderTests -q
    $initialExitCode = $LASTEXITCODE

    Write-Host "[2/2] Verifying that the reference implementation passes..."
    $env:PYTHONPATH = $referenceSource
    & $Python -m pytest $publicTests $graderTests -q
    $referenceExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}

if ($initialExitCode -eq 0) {
    Write-Error "The initial implementation unexpectedly passed all tests."
    exit 1
}

if ($referenceExitCode -ne 0) {
    Write-Error "The reference implementation did not pass all tests."
    exit 1
}

Write-Host "Stage one verified: initial implementation FAILED as expected; reference implementation PASSED."
exit 0
