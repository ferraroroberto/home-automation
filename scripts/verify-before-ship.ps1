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

# ---------------------------------------------------------- progress log
# Per-test timings on disk (home-automation#778): phase markers from this
# script, START/DONE/FAILED lines from the pytest plugin tests/_progress_log.py
# (loaded with -p on the e2e run, enabled by HA_VERIFY_PROGRESS_LOG).
#
#   webapp/verify-progress.run.log  this checkout's live run, overwritten each
#                                   time -- names the active test if a run wedges.
#   webapp/verify-progress.log      in the PRIMARY checkout: every run that
#                                   reached the browser phase, appended, last
#                                   $progressHistoryRuns kept. .fleet.toml [e2e]
#                                   progress_log declares it, so /e2e-audit ranks
#                                   the suite by seconds and reads its red history.
#
# Only browser runs are appended, so a skip-tier run never displaces the
# last e2e timing; the history lives in the primary because a worktree is
# deleted when its issue ships. Both gitignored (webapp/*.log).
$progressHistoryRuns = 20
$progressHeader = "verify-before-ship run started"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$progressLog = Join-Path (Get-Location) "webapp\verify-progress.run.log"
[void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $progressLog))
[System.IO.File]::WriteAllText($progressLog, (
    "{0} {1}`n" -f $progressHeader, (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
), $utf8NoBom)
$env:HA_VERIFY_PROGRESS_LOG = $progressLog
# The plugin's elapsed column counts from here, not from each pytest process.
$env:HA_VERIFY_PROGRESS_T0 = [string][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$e2eRan = $false

# Invariant culture: a comma-decimal locale would write "+  12,3s" next to the
# plugin's "+  12.3s".
function Format-Seconds([double]$Seconds) {
    $Seconds.ToString("0.0", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Log-Progress([string]$Message) {
    [System.IO.File]::AppendAllText($progressLog, (
        "[{0} +{1,7}s] {2}`n" -f (Get-Date -Format "HH:mm:ss"), (Format-Seconds $sw.Elapsed.TotalSeconds), $Message
    ), $utf8NoBom)
}

# Append this run to the primary checkout's history, trimmed to the last
# $progressHistoryRuns runs. A named mutex serialises gates finishing at once
# in sibling worktrees, so neither append nor trim loses the other's run.
function Publish-ProgressLog {
    $commonDir = (& git rev-parse --path-format=absolute --git-common-dir 2>$null)
    if (-not $commonDir) { return }
    $historyLog = Join-Path (Split-Path -Parent $commonDir) "webapp\verify-progress.log"
    $run = [System.IO.File]::ReadAllText($progressLog, $utf8NoBom)
    $mutex = New-Object System.Threading.Mutex($false, "home-automation-verify-progress-log")
    $owned = $false
    try {
        $owned = $mutex.WaitOne(30000)
        if (-not $owned) { throw "another gate held the history lock for 30 s" }
        $old = if (Test-Path $historyLog) { [System.IO.File]::ReadAllText($historyLog, $utf8NoBom) } else { "" }
        $runs = [regex]::Split($old + $run, "(?m)^(?=$progressHeader )") | Where-Object { $_.Trim() }
        $kept = @($runs | Select-Object -Last $progressHistoryRuns)
        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $historyLog))
        [System.IO.File]::WriteAllText($historyLog, ($kept -join ""), $utf8NoBom)
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host ">> $Name" -ForegroundColor Cyan
    Log-Progress "==> phase: $Name"
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        Log-Progress "FAILED: $Name (exit $LASTEXITCODE)"
        exit 1
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

$gateResult = "FAIL"
try {
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

    Log-Progress "e2e routing: tier=$tier reason=$routeReason"
    if ($tier -eq "skip") {
        Write-Host ""
        Write-Host ">> e2e routing: SKIP browser suite (no e2e surface touched)" -ForegroundColor Cyan
        Write-Host "   reason: $routeReason" -ForegroundColor DarkGray
        Write-Host "[PASS] pytest (e2e) - skipped, diff touches no e2e surface" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host ">> e2e routing: $tier" -ForegroundColor Cyan
        Write-Host "   reason: $routeReason" -ForegroundColor DarkGray
        # A surface slice over several modules arrives space-joined; pytest
        # needs each as its own argument (#784).
        $e2eArgs = @($e2eTarget -split '\s+' | Where-Object { $_ }) + @("-p", "tests._progress_log")
        foreach ($b in ($e2eBrowsers -split ',' | Where-Object { $_ })) {
            $e2eArgs += @("--browser", $b)
        }
        $label = if ($e2eBrowsers) { $e2eBrowsers } else { "suite-default" }
        $e2eRan = $true
        Invoke-Stage "pytest e2e (${tier}: $e2eTarget, $label)" { & $py -m pytest @e2eArgs }
    }

    $gateResult = "PASS"
    Write-Host ""
    Write-Host "[PASS] all checks green - safe to ship." -ForegroundColor Green
} finally {
    Log-Progress ("gate finished ({0}) after {1}s" -f $gateResult, (Format-Seconds $sw.Elapsed.TotalSeconds))
    Remove-Item Env:\HA_VERIFY_PROGRESS_LOG, Env:\HA_VERIFY_PROGRESS_T0 -ErrorAction SilentlyContinue
    if ($e2eRan) {
        try { Publish-ProgressLog } catch { Write-Host "   progress log not published: $($_.Exception.Message)" -ForegroundColor DarkGray }
    }
}
