#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-ship verification gate for home-automation. One pass/fail pipeline.

.DESCRIPTION
    Runs, fail-fast:
      1. byte-compile     — every .py under app/ src/ tests/ scripts/ custom_components/ parses
      2. pytest (non-e2e) — the fast backend suite (tests/, excluding tests/e2e)
      3. pytest (e2e)     — diff-proportionate: the browser slice is routed by
                            scripts/classify_e2e.py against the .fleet.toml [e2e]
                            rules (skip / static / full), fail-safe to full
                            (ferraroroberto/home-automation#603, project-scaffolding#180).
                            Boots its own disposable instance per tests/e2e/conftest.py.

    Anchors to the repo root, so run it from anywhere:  & .\scripts\verify-before-ship.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[FAIL] .venv not found at $py" -ForegroundColor Red
    Write-Host "       Create it and run: $py -m pip install -r requirements.txt -r requirements-dev.txt" -ForegroundColor Red
    exit 1
}

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host ">> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

Invoke-Stage "byte-compile" { & $py -m compileall -q app src tests scripts custom_components }
Invoke-Stage "pytest (unit, non-e2e)" { & $py -m pytest tests -p no:cacheprovider --ignore=tests/e2e }

# ---------------------------------------------------------------- e2e routing
# Diff-proportionate e2e routing (ferraroroberto/home-automation#603,
# project-scaffolding#180). Instead of always running the whole tests/e2e
# dual-projection suite, classify the branch's changed files vs main and run
# a browser slice proportionate to the diff: backend/docs-only -> skip the
# browser suite, inert static assets -> the narrow smoke target, real
# UI/behaviour -> the full suite. Fail-safe: a mixed/ambiguous/unrecognized
# diff (or no [e2e] table declared) runs the full suite. The path->tier rules
# live in .fleet.toml [e2e]; scripts/classify_e2e.py is the mechanism. On CI
# the full suite always runs -- the local gate is where routing is proven first.
$tier = "full"; $e2eTarget = "tests/e2e"; $e2eBrowsers = ""; $routeReason = ""
if ($env:CI -eq "true") {
    $routeReason = "CI always runs the full e2e suite"
} else {
    $classifyOut = & $py "scripts/classify_e2e.py"
    $kv = @{}
    foreach ($line in $classifyOut) {
        if ($line -match '^(E2E_[A-Z_]+)=(.*)$') { $kv[$matches[1]] = $matches[2] }
    }
    if ($kv.ContainsKey("E2E_TIER") -and $kv["E2E_TIER"]) {
        $tier = $kv["E2E_TIER"]
        $e2eTarget = $kv["E2E_PYTEST_TARGET"]
        $e2eBrowsers = $kv["E2E_BROWSERS"]
        $routeReason = $kv["E2E_REASON"]
    } else {
        $routeReason = "classifier gave no verdict -- defaulting to full (fail-safe)"
    }
}

if ($tier -eq "skip") {
    Write-Host ""
    Write-Host ">> e2e routing: SKIP browser suite (no e2e surface touched)" -ForegroundColor Cyan
    Write-Host "   reason: $routeReason" -ForegroundColor DarkGray
    Write-Host "[PASS] pytest (e2e) - skipped, diff touches no e2e surface" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host ">> e2e routing: $tier" -ForegroundColor Cyan
    Write-Host "   reason: $routeReason" -ForegroundColor DarkGray
    $e2eArgs = @($e2eTarget)
    foreach ($b in ($e2eBrowsers -split ',' | Where-Object { $_ })) {
        $e2eArgs += @("--browser", $b)
    }
    $label = if ($e2eBrowsers) { $e2eBrowsers } else { "suite-default" }
    Invoke-Stage "pytest e2e (${tier}: $e2eTarget, $label)" { & $py -m pytest @e2eArgs }
}

Write-Host ""
Write-Host "[PASS] all checks green - safe to ship." -ForegroundColor Green
