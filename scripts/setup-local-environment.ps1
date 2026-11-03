<#
.SYNOPSIS
Sets up the campus-rag local development environment and verifies it.

.DESCRIPTION
Checks prerequisites, starts the Docker engine if needed, creates .env from
.env.example, installs Python and dependencies with uv, starts PostgreSQL with
pgvector in Docker, applies database migrations, and runs the test suite.

Each step prints a numbered header and finishes with OK or FAILED. On failure
the script prints the step, the reason, and the command that fixes it, then
exits with the step number as its exit code.

The script is safe to rerun. On a working setup it changes nothing.
It never installs software and never prints secrets.

Steps and exit codes:
  1  Prerequisites (git, uv, Docker CLI, Docker Compose)
  2  Docker engine
  3  Environment file (.env)
  4  Python environment (uv sync --locked)
  5  Database container (docker compose up -d --wait db)
  6  Migrations (uv run python -m campus_rag.index.migrate)
  7  Verification (uv run pytest with CAMPUS_RAG_REQUIRE_DB=1)

See runbooks/local-environment-setup.md.

.PARAMETER ResetDatabase
Deletes the local database container and its data volume before starting a
fresh database. Use it when the local database is broken or out of sync with
the migrations. All local database data is lost.

.PARAMETER SkipTests
Skips step 7 (the test run).

.PARAMETER DockerStartTimeoutSeconds
How long to wait for the Docker engine after starting Docker Desktop.
Default: 180.

.PARAMETER PackageIndexUrl
A Python package mirror (PyPI-compatible "simple" index URL) to use when the
network blocks files.pythonhosted.org. The script installs the exact packages
and hashes pinned in uv.lock from the mirror, then confirms the environment
matches uv.lock with 'uv sync --locked --offline'. uv.lock is not changed.
Credentials in the URL are never printed.

.EXAMPLE
.\scripts\setup-local-environment.ps1

Sets up the environment and runs the tests.

.EXAMPLE
.\scripts\setup-local-environment.ps1 -SkipTests

Sets up the environment without running the tests.

.EXAMPLE
.\scripts\setup-local-environment.ps1 -ResetDatabase

Deletes the local database and its data, then sets everything up again.

.EXAMPLE
.\scripts\setup-local-environment.ps1 -PackageIndexUrl https://mirror.example.com/pypi/simple/

Installs the locked Python packages from a package mirror.

.NOTES
Runs under Windows PowerShell 5.1 and PowerShell 7. Requires no administrator
rights. Exit code 0 means success; exit codes 1-7 identify the failed step.
#>
[CmdletBinding()]
param(
    [switch]$ResetDatabase,
    [switch]$SkipTests,
    [ValidateRange(0, 3600)]
    [int]$DockerStartTimeoutSeconds = 180,
    [ValidatePattern('^https?://\S+$')]
    [string]$PackageIndexUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunbookPath = 'runbooks\local-environment-setup.md'
$TotalSteps = 7
$DefaultDbPort = 5433
# uv output that means the network blocks the default package host.
$NetworkFailurePattern = 'HandshakeFailure|SEC_E_|client error \(Connect\)|error sending request|files\.pythonhosted\.org'
$ScriptTimer = [System.Diagnostics.Stopwatch]::StartNew()

# Resolved executables and settings shared between steps.
$script:UvExe = $null
$script:DockerExe = $null
$script:DbConfig = $null

function New-StepFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter(Mandatory = $true)][string]$Fix
    )
    $exception = New-Object System.Exception $Reason
    $exception.Data['CampusRagFix'] = $Fix
    return $exception
}

function Write-Detail {
    param([string]$Message)
    Write-Host "    $Message"
}

function Write-Note {
    param([string]$Message)
    Write-Host "    NOTE: $Message" -ForegroundColor Yellow
}

function Get-RedactedText {
    # Removes credentials (user:password@ or token@) from any URLs in the text.
    param([string]$Text)
    if (-not $Text) {
        return $Text
    }
    return ($Text -replace '(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@', '$1')
}

function Invoke-Native {
    # Runs a native command, echoes its output (stdout and stderr) indented,
    # and returns the exit code and output lines. Never throws on stderr output.
    # Output lines are stripped of URL credentials before they are echoed or returned.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$Quiet
    )
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    & $FilePath @ArgumentList 2>&1 | ForEach-Object {
        $line = Get-RedactedText $_.ToString()
        if ($line -eq 'System.Management.Automation.RemoteException') {
            # Windows PowerShell 5.1 renders an empty stderr line as the exception type name.
            $line = ''
        }
        $lines.Add($line)
        if (-not $Quiet) {
            Write-Host "    $line"
        }
    }
    $exitCode = $LASTEXITCODE
    return New-Object PSObject -Property @{ ExitCode = $exitCode; Output = $lines.ToArray() }
}

function Get-LastLine {
    param([string[]]$Lines)
    $nonEmpty = @($Lines | Where-Object { $_ -and $_.Trim() })
    if ($nonEmpty.Count -eq 0) {
        return ''
    }
    return $nonEmpty[-1].Trim()
}

function Read-DotEnv {
    # Minimal KEY=VALUE parser: ignores blank lines and # comments, strips matching quotes.
    param([Parameter(Mandatory = $true)][string]$Path)
    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) {
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) {
            continue
        }
        $key = $line.Substring(0, $separator).Trim()
        if ($key.StartsWith('export ')) {
            $key = $key.Substring(7).Trim()
        }
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Get-ConfigValue {
    # Process environment variables override .env, matching Docker Compose and campus_rag.config.
    param(
        [hashtable]$DotEnv,
        [string]$Name,
        [string]$Default
    )
    $fromProcess = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($fromProcess) {
        return New-Object PSObject -Property @{ Value = $fromProcess; Source = 'environment variable' }
    }
    if ($DotEnv.ContainsKey($Name) -and $DotEnv[$Name]) {
        return New-Object PSObject -Property @{ Value = $DotEnv[$Name]; Source = '.env' }
    }
    return New-Object PSObject -Property @{ Value = $Default; Source = 'default' }
}

function Get-PortListener {
    # Returns the processes listening on a local TCP port, or nothing when the port is free.
    param([Parameter(Mandatory = $true)][int]$Port)
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return
    }
    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $processIds = @($connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    foreach ($processId in $processIds) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        $name = 'unknown'
        if ($process) {
            $name = $process.ProcessName
        }
        New-Object PSObject -Property @{ ProcessId = $processId; ProcessName = $name }
    }
}

function Test-DockerEngine {
    $result = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('info', '--format', '{{.OSType}}') -Quiet
    return New-Object PSObject -Property @{
        Running = ($result.ExitCode -eq 0)
        OSType  = (Get-LastLine $result.Output)
        Error   = (Get-LastLine $result.Output)
    }
}

function Invoke-Step1Prerequisites {
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $git) {
        throw (New-StepFailure -Reason 'git is not installed or not on PATH.' -Fix 'winget install --id Git.Git -e   (then open a new terminal and rerun)')
    }
    $gitVersion = Invoke-Native -FilePath $git.Source -ArgumentList @('--version') -Quiet
    Write-Detail "git:    $(Get-LastLine $gitVersion.Output)"

    $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $uv) {
        throw (New-StepFailure -Reason 'uv is not installed or not on PATH.' -Fix 'winget install --id astral-sh.uv -e   (then open a new terminal and rerun)')
    }
    $script:UvExe = $uv.Source
    $uvVersion = Invoke-Native -FilePath $script:UvExe -ArgumentList @('--version') -Quiet
    if ($uvVersion.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "uv is on PATH but 'uv --version' failed: $(Get-LastLine $uvVersion.Output)" -Fix 'winget upgrade --id astral-sh.uv -e   (then open a new terminal and rerun)')
    }
    Write-Detail "uv:     $(Get-LastLine $uvVersion.Output)"

    $docker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($docker) {
        $script:DockerExe = $docker.Source
    }
    else {
        $dockerBin = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
        $candidate = Join-Path $dockerBin 'docker.exe'
        if (-not (Test-Path -LiteralPath $candidate)) {
            throw (New-StepFailure -Reason 'Docker is not installed (docker is not on PATH and Docker Desktop was not found).' -Fix 'winget install --id Docker.DockerDesktop -e   (then start Docker Desktop, open a new terminal, and rerun)')
        }
        $env:PATH = "$dockerBin;$env:PATH"
        $script:DockerExe = $candidate
        Write-Note "docker was not on PATH; using $candidate for this session. Open a new terminal to pick up PATH changes from the Docker Desktop installer."
    }
    $dockerVersion = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('--version') -Quiet
    Write-Detail "docker: $(Get-LastLine $dockerVersion.Output)"

    $composeVersion = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'version') -Quiet
    if ($composeVersion.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "'docker compose version' failed: $(Get-LastLine $composeVersion.Output)" -Fix 'winget upgrade --id Docker.DockerDesktop -e   (Docker Desktop includes Docker Compose v2)')
    }
    Write-Detail "compose: $(Get-LastLine $composeVersion.Output)"
}

function Invoke-Step2DockerEngine {
    $engine = Test-DockerEngine
    if (-not $engine.Running) {
        $desktopExe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        $desktopInstalled = Test-Path -LiteralPath $desktopExe
        if ($desktopInstalled) {
            $desktopProcess = Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue
            if ($desktopProcess) {
                Write-Detail 'Docker Desktop is running, but the Docker engine is not reachable yet.'
            }
            else {
                Write-Detail 'Starting Docker Desktop...'
                Start-Process -FilePath $desktopExe | Out-Null
            }
            Write-Detail "Waiting up to $DockerStartTimeoutSeconds seconds for the Docker engine..."
            $timer = [System.Diagnostics.Stopwatch]::StartNew()
            $lastReport = 0
            while (-not $engine.Running -and $timer.Elapsed.TotalSeconds -lt $DockerStartTimeoutSeconds) {
                Start-Sleep -Seconds 3
                $engine = Test-DockerEngine
                $elapsed = [int]$timer.Elapsed.TotalSeconds
                if (-not $engine.Running -and ($elapsed - $lastReport) -ge 15) {
                    Write-Detail "Still waiting ($elapsed s)..."
                    $lastReport = $elapsed
                }
            }
        }

        if (-not $engine.Running) {
            $reason = 'the Docker engine is not reachable'
            if ($desktopInstalled) {
                $reason = "the Docker engine did not respond within $DockerStartTimeoutSeconds seconds"
            }
            if ($engine.Error) {
                $reason = "$reason ($($engine.Error.TrimEnd('.')))"
            }
            $fix = 'Start Docker Desktop and wait until it shows "Engine running", then rerun. If it does not start, check WSL 2 with: wsl --status'
            if ($env:DOCKER_HOST) {
                $reason = "$reason. DOCKER_HOST is set to '$($env:DOCKER_HOST)'"
                $fix = 'Remove-Item Env:DOCKER_HOST   (then rerun; Docker Desktop does not need DOCKER_HOST)'
            }
            elseif (-not $desktopInstalled) {
                $fix = 'Start your Docker engine, or install Docker Desktop: winget install --id Docker.DockerDesktop -e'
            }
            throw (New-StepFailure -Reason "$reason." -Fix $fix)
        }
    }

    if ($engine.OSType -ne 'linux') {
        throw (New-StepFailure -Reason "Docker is running $($engine.OSType) containers; the database image needs Linux containers." -Fix "& `"$env:ProgramFiles\Docker\Docker\DockerCli.exe`" -SwitchLinuxEngine   (or right-click the Docker tray icon and choose 'Switch to Linux containers')")
    }
    Write-Detail 'Docker engine is running (Linux containers).'
}

function Invoke-Step3EnvironmentFile {
    $envFile = Join-Path $RepoRoot '.env'
    $exampleFile = Join-Path $RepoRoot '.env.example'
    if (Test-Path -LiteralPath $envFile) {
        Write-Detail 'Using existing .env (left unchanged).'
    }
    else {
        if (-not (Test-Path -LiteralPath $exampleFile)) {
            throw (New-StepFailure -Reason '.env and .env.example are both missing.' -Fix 'git checkout -- .env.example   (then rerun)')
        }
        Copy-Item -LiteralPath $exampleFile -Destination $envFile
        Write-Detail 'Created .env from .env.example.'
    }

    $dotEnv = Read-DotEnv -Path $envFile
    $port = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_DB_PORT' -Default ([string]$DefaultDbPort)
    $portNumber = 0
    if (-not [int]::TryParse($port.Value, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) {
        throw (New-StepFailure -Reason "CAMPUS_RAG_DB_PORT is '$($port.Value)' (from $($port.Source)); it must be a port number from 1 to 65535." -Fix "Set CAMPUS_RAG_DB_PORT=$DefaultDbPort in .env (and remove any CAMPUS_RAG_DB_PORT environment variable)")
    }
    $hostName = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_DB_HOST' -Default '127.0.0.1'
    $dbName = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_DB_NAME' -Default 'campus_rag'
    $url = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_DATABASE_URL' -Default ''

    $target = "$($hostName.Value):$portNumber/$($dbName.Value)"
    $targetSource = 'CAMPUS_RAG_DB_* settings'
    if ($url.Value) {
        $targetSource = "CAMPUS_RAG_DATABASE_URL ($($url.Source))"
        try {
            $uri = New-Object System.Uri $url.Value
            $target = "$($uri.Host):$($uri.Port)/$($uri.AbsolutePath.TrimStart('/'))"
        }
        catch {
            $target = '(unparsed CAMPUS_RAG_DATABASE_URL)'
        }
        Write-Note "CAMPUS_RAG_DATABASE_URL is set; migrations and tests connect to $target instead of the CAMPUS_RAG_DB_* values."
    }
    foreach ($setting in @($port, $hostName, $dbName)) {
        if ($setting.Source -eq 'environment variable') {
            Write-Note 'A CAMPUS_RAG_DB_* environment variable overrides .env in this terminal.'
            break
        }
    }

    $script:DbConfig = New-Object PSObject -Property @{
        Port         = $portNumber
        PortSource   = $port.Source
        Target       = $target
        TargetSource = $targetSource
    }
    Write-Detail "Database port: $portNumber (from $($port.Source))"
}

function Assert-UsableLockFile {
    # Turns uv lockfile errors in the output into step failures.
    param([string]$Output)
    if ($Output -match 'Unable to find lockfile') {
        throw (New-StepFailure -Reason 'uv.lock is missing.' -Fix 'git checkout -- uv.lock   (then rerun)')
    }
    if ($Output -match 'needs to be updated') {
        throw (New-StepFailure -Reason 'uv.lock is out of date with pyproject.toml.' -Fix 'uv lock   (on a network that can reach PyPI; then rerun the script and commit the updated uv.lock)')
    }
}

function Get-PipIndexUrl {
    # Returns the index-url from pip's configuration (for example C:\ProgramData\pip\pip.ini)
    # with any credentials removed, or $null when pip or the setting is missing.
    $pip = Get-Command pip -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pip) {
        return $null
    }
    $result = Invoke-Native -FilePath $pip.Source -ArgumentList @('config', 'list') -Quiet
    if ($result.ExitCode -ne 0) {
        return $null
    }
    foreach ($line in $result.Output) {
        if ($line -match "^[\w:.-]*\.index-url\s*=\s*'?([^'\s]+)'?\s*$") {
            return $Matches[1]
        }
    }
    return $null
}

function Get-ProjectEnvironmentPath {
    # The environment 'uv sync' manages: UV_PROJECT_ENVIRONMENT when set, otherwise .venv.
    $configured = $env:UV_PROJECT_ENVIRONMENT
    if (-not $configured) {
        return Join-Path $RepoRoot '.venv'
    }
    if ([System.IO.Path]::IsPathRooted($configured)) {
        return $configured
    }
    return Join-Path $RepoRoot $configured
}

function Invoke-LockedSync {
    $result = Invoke-Native -FilePath $script:UvExe -ArgumentList @('sync', '--locked')
    if ($result.ExitCode -eq 0) {
        return
    }
    $output = $result.Output -join "`n"
    Assert-UsableLockFile -Output $output
    if ($output -match $NetworkFailurePattern) {
        $mirror = Get-PipIndexUrl
        $suggestion = '"<your package mirror URL>"'
        if ($mirror) {
            $suggestion = $mirror
            Write-Detail "Found a package mirror in your pip configuration: $mirror"
        }
        throw (New-StepFailure -Reason 'cannot download packages from files.pythonhosted.org (this network blocks it, or the machine is offline).' -Fix ".\scripts\setup-local-environment.ps1 -PackageIndexUrl $suggestion   (see 'Restricted networks' in $RunbookPath)")
    }
    throw (New-StepFailure -Reason "uv sync --locked failed with exit code $($result.ExitCode): $(Get-LastLine $result.Output)" -Fix 'Resolve the uv error shown above, then rerun. Reproduce with: uv sync --locked')
}

function Invoke-MirrorSync {
    # Installs exactly the packages and hashes pinned in uv.lock from a mirror, without changing uv.lock.
    $mirror = Get-RedactedText $PackageIndexUrl
    $environment = Get-ProjectEnvironmentPath
    $requirements = Join-Path ([System.IO.Path]::GetTempPath()) ('campus-rag-requirements-{0}.txt' -f [guid]::NewGuid().ToString('N'))
    Write-Detail "Installing the packages pinned in uv.lock from $mirror"
    try {
        Write-Detail '> uv export --frozen --format requirements-txt --no-emit-project'
        $export = Invoke-Native -FilePath $script:UvExe -ArgumentList @('export', '--frozen', '--format', 'requirements-txt', '--no-emit-project', '--quiet', '-o', $requirements) -Quiet
        if ($export.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $requirements)) {
            Assert-UsableLockFile -Output ($export.Output -join "`n")
            throw (New-StepFailure -Reason "uv export failed with exit code $($export.ExitCode): $(Get-LastLine $export.Output)" -Fix 'uv export --frozen --format requirements-txt --no-emit-project   (read its error, then rerun)')
        }

        if (-not (Test-Path -LiteralPath (Join-Path $environment 'pyvenv.cfg'))) {
            Write-Detail "> uv venv $environment"
            $venv = Invoke-Native -FilePath $script:UvExe -ArgumentList @('venv', $environment)
            if ($venv.ExitCode -ne 0) {
                throw (New-StepFailure -Reason "could not create the Python environment: $(Get-LastLine $venv.Output)" -Fix 'Check access to github.com (uv downloads Python from there), then rerun')
            }
        }

        Write-Detail "> uv pip sync <requirements> --index-url $mirror --require-hashes"
        $sync = Invoke-Native -FilePath $script:UvExe -ArgumentList @('pip', 'sync', $requirements, '--python', $environment, '--index-url', $PackageIndexUrl, '--require-hashes')
        if ($sync.ExitCode -ne 0) {
            $output = $sync.Output -join "`n"
            if ($output -match '(?i)hash mismatch|hashes do not match') {
                throw (New-StepFailure -Reason 'the mirror served files whose hashes differ from uv.lock.' -Fix 'Use a mirror that serves unmodified PyPI files, then rerun with -PackageIndexUrl')
            }
            if ($output -match $NetworkFailurePattern) {
                throw (New-StepFailure -Reason "cannot download packages from the mirror $mirror." -Fix 'Check the mirror URL and your network access, then rerun with -PackageIndexUrl')
            }
            throw (New-StepFailure -Reason "uv pip sync failed with exit code $($sync.ExitCode): $(Get-LastLine $sync.Output)" -Fix 'Read the uv error above, then rerun with -PackageIndexUrl')
        }

        Write-Detail "> uv pip install --no-deps -e . --index-url $mirror"
        $project = Invoke-Native -FilePath $script:UvExe -ArgumentList @('pip', 'install', '--python', $environment, '--no-deps', '-e', '.', '--index-url', $PackageIndexUrl)
        if ($project.ExitCode -ne 0) {
            throw (New-StepFailure -Reason "installing the campus-rag project failed with exit code $($project.ExitCode): $(Get-LastLine $project.Output)" -Fix 'Read the uv error above, then rerun with -PackageIndexUrl')
        }

        Write-Detail '> uv sync --locked --offline   (confirms the environment matches uv.lock)'
        $verify = Invoke-Native -FilePath $script:UvExe -ArgumentList @('sync', '--locked', '--offline')
        if ($verify.ExitCode -ne 0) {
            Assert-UsableLockFile -Output ($verify.Output -join "`n")
            throw (New-StepFailure -Reason "the Python environment does not match uv.lock after installing from the mirror: $(Get-LastLine $verify.Output)" -Fix "Remove-Item -Recurse -Force '$environment'   (then rerun with -PackageIndexUrl)")
        }
    }
    finally {
        Remove-Item -LiteralPath $requirements -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Step4PythonEnvironment {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
        throw (New-StepFailure -Reason 'pyproject.toml is missing from the repository root.' -Fix 'git pull   (make sure you have the latest main branch)')
    }
    if ($PackageIndexUrl) {
        Invoke-MirrorSync
    }
    else {
        Invoke-LockedSync
    }
}

function Invoke-Step5DatabaseContainer {
    $port = $script:DbConfig.Port
    if ($ResetDatabase) {
        Write-Host '    WARNING: -ResetDatabase deletes the local database container and ALL of its data.' -ForegroundColor Yellow
        $down = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'down', '--volumes')
        if ($down.ExitCode -ne 0) {
            throw (New-StepFailure -Reason "docker compose down --volumes failed with exit code $($down.ExitCode): $(Get-LastLine $down.Output)" -Fix 'docker compose down --volumes   (read its error, then rerun)')
        }
        Write-Detail 'Local database and volume deleted.'
    }

    $running = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'ps', '--status', 'running', '-q', 'db') -Quiet
    if ($running.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "docker compose ps failed with exit code $($running.ExitCode): $(Get-LastLine $running.Output)" -Fix 'docker compose ps   (read its error, then rerun)')
    }
    $dbRunning = [bool](Get-LastLine $running.Output)
    if (-not $dbRunning) {
        $listeners = @(Get-PortListener -Port $port)
        if ($listeners.Count -gt 0) {
            $owners = ($listeners | ForEach-Object { "$($_.ProcessName) (PID $($_.ProcessId))" }) -join ', '
            $fix = "Stop that process, or set CAMPUS_RAG_DB_PORT to a free port (for example 5434) in .env, then rerun."
            if ($owners -match 'docker|wslrelay|vpnkit') {
                $fix = "Another container publishes port $port; find it with: docker ps --filter publish=$port   Stop it, or set CAMPUS_RAG_DB_PORT to a free port in .env, then rerun."
            }
            throw (New-StepFailure -Reason "port $port is already in use by $owners." -Fix $fix)
        }
    }

    $up = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'up', '-d', '--wait', 'db')
    if ($up.ExitCode -ne 0) {
        $output = $up.Output -join "`n"
        if ($output -match 'port is already allocated|address already in use|bind') {
            throw (New-StepFailure -Reason "the database could not bind port $port; another process or container is using it." -Fix "Set CAMPUS_RAG_DB_PORT to a free port (for example 5434) in .env, then rerun. Find containers with: docker ps --filter publish=$port")
        }
        if ($output -match 'unhealthy') {
            throw (New-StepFailure -Reason 'the database container started but failed its health check.' -Fix 'docker compose logs db   (read the error; if the data volume is corrupt, rerun with -ResetDatabase)')
        }
        throw (New-StepFailure -Reason "docker compose up -d --wait db failed with exit code $($up.ExitCode): $(Get-LastLine $up.Output)" -Fix 'Check network access for the image pull and read the error above. Inspect with: docker compose logs db')
    }
    Write-Detail "Database is healthy on 127.0.0.1:$port."
}

function Invoke-Step6Migrations {
    $result = Invoke-Native -FilePath $script:UvExe -ArgumentList @('run', 'python', '-m', 'campus_rag.index.migrate')
    switch ($result.ExitCode) {
        0 { return }
        2 {
            throw (New-StepFailure -Reason "cannot connect to the database at $($script:DbConfig.Target)." -Fix 'docker compose ps db   (it should show "healthy"); check the CAMPUS_RAG_DB_* values in .env match the running container, then rerun')
        }
        3 {
            throw (New-StepFailure -Reason "a migration failed: $(Get-LastLine $result.Output)" -Fix 'Fix the migration error shown above. If the local database is out of sync, run: .\scripts\setup-local-environment.ps1 -ResetDatabase')
        }
        default {
            throw (New-StepFailure -Reason "the migration command failed with exit code $($result.ExitCode): $(Get-LastLine $result.Output)" -Fix 'Read the error above. Reproduce with: uv run python -m campus_rag.index.migrate')
        }
    }
}

function Invoke-Step7Verification {
    if ($SkipTests) {
        Write-Detail 'Skipped (-SkipTests).'
        return
    }
    $previous = [Environment]::GetEnvironmentVariable('CAMPUS_RAG_REQUIRE_DB', 'Process')
    [Environment]::SetEnvironmentVariable('CAMPUS_RAG_REQUIRE_DB', '1', 'Process')
    try {
        $result = Invoke-Native -FilePath $script:UvExe -ArgumentList @('run', 'pytest')
    }
    finally {
        [Environment]::SetEnvironmentVariable('CAMPUS_RAG_REQUIRE_DB', $previous, 'Process')
    }
    if ($result.ExitCode -ne 0) {
        $summary = Get-LastLine $result.Output
        throw (New-StepFailure -Reason "tests failed (pytest exit code $($result.ExitCode)): $summary" -Fix "Read the failures above. Rerun with: `$env:CAMPUS_RAG_REQUIRE_DB='1'; uv run pytest")
    }
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][int]$Number,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-Host ''
    Write-Host ("[{0}/{1}] {2}  (t+{3:N1}s)" -f $Number, $TotalSteps, $Name, $ScriptTimer.Elapsed.TotalSeconds) -ForegroundColor Cyan
    $stepTimer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $null = & $Action
    }
    catch {
        $exception = $_.Exception
        $fix = $null
        if ($exception.Data.Contains('CampusRagFix')) {
            $fix = [string]$exception.Data['CampusRagFix']
        }
        Write-Host ("[{0}/{1}] FAILED in {2:N1}s" -f $Number, $TotalSteps, $stepTimer.Elapsed.TotalSeconds) -ForegroundColor Red
        Write-Host ''
        Write-Host "FAILED at step $Number ($Name): $($exception.Message)" -ForegroundColor Red
        if ($fix) {
            Write-Host "Fix: $fix" -ForegroundColor Yellow
        }
        else {
            # Unexpected error: show where it happened to help diagnosis.
            Write-Host "Location: $($_.InvocationInfo.PositionMessage)"
            Write-Host "Fix: rerun the script; if it fails again, report this output. Runbook: $RunbookPath" -ForegroundColor Yellow
        }
        return $false
    }
    Write-Host ("[{0}/{1}] OK in {2:N1}s" -f $Number, $TotalSteps, $stepTimer.Elapsed.TotalSeconds) -ForegroundColor Green
    return $true
}

$steps = @(
    @{ Name = 'Prerequisites'; Action = ${function:Invoke-Step1Prerequisites} },
    @{ Name = 'Docker engine'; Action = ${function:Invoke-Step2DockerEngine} },
    @{ Name = 'Environment file'; Action = ${function:Invoke-Step3EnvironmentFile} },
    @{ Name = 'Python environment'; Action = ${function:Invoke-Step4PythonEnvironment} },
    @{ Name = 'Database container'; Action = ${function:Invoke-Step5DatabaseContainer} },
    @{ Name = 'Migrations'; Action = ${function:Invoke-Step6Migrations} },
    @{ Name = 'Verification'; Action = ${function:Invoke-Step7Verification} }
)

$exitCode = 0
Push-Location -LiteralPath $RepoRoot
try {
    Write-Host "campus-rag local environment setup ($RepoRoot)"
    for ($index = 0; $index -lt $steps.Count; $index++) {
        $number = $index + 1
        $succeeded = Invoke-Step -Number $number -Name $steps[$index].Name -Action $steps[$index].Action
        if (-not $succeeded) {
            $exitCode = $number
            break
        }
    }

    if ($exitCode -eq 0) {
        $pythonVersion = Invoke-Native -FilePath $script:UvExe -ArgumentList @('run', 'python', '--version') -Quiet
        $pythonText = Get-LastLine $pythonVersion.Output
        if ($pythonVersion.ExitCode -ne 0) {
            $pythonText = 'unknown (uv run python --version failed)'
        }
        $testsText = 'passed'
        if ($SkipTests) {
            $testsText = 'skipped (-SkipTests)'
        }
        Write-Host ''
        Write-Host ("Setup complete in {0:N1}s." -f $ScriptTimer.Elapsed.TotalSeconds) -ForegroundColor Green
        Write-Host "  Database:  $($script:DbConfig.Target) (from $($script:DbConfig.TargetSource))"
        Write-Host "  Python:    $pythonText"
        Write-Host "  Tests:     $testsText"
        Write-Host '  Stop DB:   docker compose stop db'
        Write-Host '  Reset DB:  .\scripts\setup-local-environment.ps1 -ResetDatabase   (deletes local data)'
        Write-Host "  Runbook:   $RunbookPath"
    }
}
finally {
    Pop-Location
}
exit $exitCode
