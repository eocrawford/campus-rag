<#
.SYNOPSIS
Updates uv.lock through a package mirror while keeping canonical PyPI URLs in the committed lock.

.DESCRIPTION
For networks that block files.pythonhosted.org and require a PyPI mirror. After you edit
pyproject.toml, this script:
  1. Copies pyproject.toml, uv.lock, .python-version, README.md, and src\ to a temporary folder.
  2. Runs `uv lock` there with UV_DEFAULT_INDEX set to the mirror.
  3. Runs scripts\canonicalize_lock.py, which rewrites every mirror URL to its pypi.org or
     files.pythonhosted.org URL after checking sha256 and size against pypi.org.
  4. Copies the canonical uv.lock back, prints the package changes, and checks it with
     `uv lock --check --offline`.
The repository's own uv.lock is never locked against the mirror directly. Then install with
.\scripts\setup-local-environment.ps1 -PackageIndexUrl <mirror>, run the tests, and commit
pyproject.toml and uv.lock together. See runbooks\update-dependencies.md.

.PARAMETER PackageIndexUrl
The mirror's PyPI-compatible "simple" index URL. Default: global.index-url from `pip config list`.

.EXAMPLE
.\scripts\update-lock-via-mirror.ps1 -PackageIndexUrl https://mirror.example.com/pypi/simple/
#>
[CmdletBinding()]
param(
    [ValidatePattern('^https?://\S+$')]
    [string]$PackageIndexUrl
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Files = 'pyproject.toml', 'uv.lock', '.python-version', 'README.md'

function Fail([string]$Reason, [string]$Fix) {
    Write-Host "FAILED: $Reason" -ForegroundColor Red
    if ($Fix) { Write-Host "Fix: $Fix" }
    exit 1
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv is not installed.' 'winget install --id astral-sh.uv -e' }
if (-not $PackageIndexUrl) {
    $configured = $null
    if (Get-Command pip -ErrorAction SilentlyContinue) {
        $configured = pip config list 2>$null | Select-String "global\.index-url='(.+)'"
    }
    if (-not $configured) { Fail 'No package mirror is configured in pip.' '.\scripts\update-lock-via-mirror.ps1 -PackageIndexUrl <mirror simple URL>' }
    $PackageIndexUrl = $configured.Matches[0].Groups[1].Value
}
$shownUrl = $PackageIndexUrl -replace '(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@', '$1'
foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $file))) { Fail "$file is missing from $RepoRoot." 'git pull' }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ('campus-rag-lock-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    foreach ($file in $Files) { Copy-Item -LiteralPath (Join-Path $RepoRoot $file) -Destination $work }
    Copy-Item -Recurse -LiteralPath (Join-Path $RepoRoot 'src') -Destination (Join-Path $work 'src')
    $before = [IO.File]::ReadAllText((Join-Path $work 'uv.lock'))

    Write-Host "Locking through $shownUrl ..."
    Push-Location $work
    try {
        $env:UV_DEFAULT_INDEX = $PackageIndexUrl
        uv lock
        if ($LASTEXITCODE -ne 0) { Fail 'uv lock failed against the mirror.' 'Check the mirror URL and pyproject.toml, then rerun.' }
        Remove-Item Env:UV_DEFAULT_INDEX
        uv run --no-project --python 3.12 python (Join-Path $PSScriptRoot 'canonicalize_lock.py') uv.lock
        if ($LASTEXITCODE -ne 0) { Fail 'The lock could not be rewritten to canonical PyPI URLs; the repository is unchanged.' 'Read the ERROR line above.' }
    } finally {
        Remove-Item Env:UV_DEFAULT_INDEX -ErrorAction SilentlyContinue
        Pop-Location
    }

    $after = [IO.File]::ReadAllText((Join-Path $work 'uv.lock'))
    if ($after -eq $before) { Write-Host 'uv.lock is already up to date; nothing changed.'; return }
    $packages = {
        param($text)
        [regex]::Matches($text, '\[\[package\]\]\r?\nname = "([^"]+)"\r?\nversion = "([^"]+)"') |
            ForEach-Object { "$($_.Groups[1].Value)==$($_.Groups[2].Value)" }
    }
    $old = @(& $packages $before); $new = @(& $packages $after)
    $removed = @($old | Where-Object { $new -notcontains $_ }); $added = @($new | Where-Object { $old -notcontains $_ })
    Write-Host ('Removed: ' + $(if ($removed) { $removed -join ', ' } else { 'none' }))
    Write-Host ('Added:   ' + $(if ($added) { $added -join ', ' } else { 'none' }))

    Copy-Item -LiteralPath (Join-Path $work 'uv.lock') -Destination (Join-Path $RepoRoot 'uv.lock') -Force
    Push-Location $RepoRoot
    try {
        uv lock --check --offline
        if ($LASTEXITCODE -ne 0) { Fail 'uv lock --check failed after the update.' 'Report this; restore with: git checkout -- uv.lock' }
    } finally { Pop-Location }
    Write-Host "Updated uv.lock. Next: .\scripts\setup-local-environment.ps1 -PackageIndexUrl $shownUrl, run the tests, then commit pyproject.toml and uv.lock."
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}
