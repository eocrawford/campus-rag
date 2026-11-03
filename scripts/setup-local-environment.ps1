<#
.SYNOPSIS
Sets up the campus-rag local development environment and verifies it.

.DESCRIPTION
Checks prerequisites, starts the Docker engine if needed, creates .env from
.env.example, installs Python and dependencies with uv, starts PostgreSQL with
pgvector and the Ollama model server in Docker, pulls and checks the embedding
and chat models, applies database migrations, and runs the test suite.

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
  6  Model server (docker compose up -d --wait ollama, model pulls, embedding and chat checks)
  7  Migrations (uv run python -m campus_rag.index.migrate)
  8  Verification (uv run pytest with CAMPUS_RAG_REQUIRE_DB=1)

See runbooks/local-environment-setup.md.

.PARAMETER ResetDatabase
Deletes the local database container and its data volume before starting a
fresh database. Use it when the local database is broken or out of sync with
the migrations. All local database data is lost. Pulled embedding models are
kept.

.PARAMETER SkipModelServer
Skips step 6 (the Ollama model server). Commands that embed text or call chat
models fail until the script runs without this switch.

.PARAMETER SkipChatModels
Skips pulling and checking the chat models (answer, rewrite, and judge) in
step 6; the embedding model is still pulled and checked. Use it when you only
need ingestion. The ask, evaluate, and api commands fail with exit code 7 until
the script runs without this switch.

.PARAMETER SkipTests
Skips step 8 (the test run).

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
.\scripts\setup-local-environment.ps1 -SkipChatModels

Sets up everything ingestion needs and skips the chat models, saving about 11.5 GB of downloads.

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
rights. Exit code 0 means success; exit codes 1-8 identify the failed step.
#>
[CmdletBinding()]
param(
    [switch]$ResetDatabase,
    [switch]$SkipModelServer,
    [switch]$SkipChatModels,
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
$TotalSteps = 8
$DefaultDbPort = 5433
$DefaultOllamaPort = 11434
$DefaultEmbeddingBaseUrl = 'http://127.0.0.1:11434/v1'
$DefaultEmbeddingModel = 'nomic-embed-text'
$DefaultEmbeddingDimensions = 768
$DefaultDocumentPrefix = 'search_document: '
$DefaultChatBaseUrl = 'http://127.0.0.1:11434/v1'
$DefaultAnswerModel = 'qwen2.5:7b-instruct'
$DefaultRewriteModel = 'qwen2.5:3b-instruct'
$DefaultJudgeModel = 'llama3.1:8b'
$DefaultChatTimeoutSeconds = 300
# Approximate download sizes of the default models, shown before a pull.
$KnownModelSizes = @{
    'nomic-embed-text'    = '275 MB'
    'qwen2.5:7b-instruct' = '4.7 GB'
    'qwen2.5:3b-instruct' = '1.9 GB'
    'llama3.1:8b'         = '4.9 GB'
}
# uv output that means the network blocks the default package host.
$NetworkFailurePattern = 'HandshakeFailure|SEC_E_|client error \(Connect\)|error sending request|files\.pythonhosted\.org'
$ScriptTimer = [System.Diagnostics.Stopwatch]::StartNew()

# Resolved executables and settings shared between steps.
$script:UvExe = $null
$script:DockerExe = $null
$script:DbConfig = $null
$script:ModelConfig = $null
$script:ChatConfig = $null

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
    # An empty value means "use the default" unless -AllowEmpty is set.
    param(
        [hashtable]$DotEnv,
        [string]$Name,
        [string]$Default,
        [switch]$AllowEmpty
    )
    $fromProcess = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($fromProcess) {
        return New-Object PSObject -Property @{ Value = $fromProcess; Source = 'environment variable' }
    }
    if ($DotEnv.ContainsKey($Name) -and ($AllowEmpty -or $DotEnv[$Name])) {
        return New-Object PSObject -Property @{ Value = $DotEnv[$Name]; Source = '.env' }
    }
    return New-Object PSObject -Property @{ Value = $Default; Source = 'default' }
}

function ConvertTo-PortNumber {
    # Validates a port setting returned by Get-ConfigValue and returns it as an integer.
    param(
        [Parameter(Mandatory = $true)]$Setting,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Default
    )
    $number = 0
    if (-not [int]::TryParse($Setting.Value, [ref]$number) -or $number -lt 1 -or $number -gt 65535) {
        throw (New-StepFailure -Reason "$Name is '$($Setting.Value)' (from $($Setting.Source)); it must be a port number from 1 to 65535." -Fix "Set $Name=$Default in .env (and remove any $Name environment variable)")
    }
    return $number
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
    $envFileExisted = Test-Path -LiteralPath $envFile
    if ($envFileExisted) {
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
    if ($envFileExisted -and (Test-Path -LiteralPath $exampleFile)) {
        $exampleKeys = @((Read-DotEnv -Path $exampleFile).Keys)
        $missingKeys = @($exampleKeys | Where-Object { -not $dotEnv.ContainsKey($_) } | Sort-Object)
        if ($missingKeys.Count -gt 0) {
            Write-Note ".env does not set $($missingKeys -join ', '). Their defaults apply; to override one, copy its line from .env.example into .env."
        }
    }

    $port = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_DB_PORT' -Default ([string]$DefaultDbPort)
    $portNumber = ConvertTo-PortNumber -Setting $port -Name 'CAMPUS_RAG_DB_PORT' -Default $DefaultDbPort
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

    $ollamaPort = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_OLLAMA_PORT' -Default ([string]$DefaultOllamaPort)
    $ollamaPortNumber = ConvertTo-PortNumber -Setting $ollamaPort -Name 'CAMPUS_RAG_OLLAMA_PORT' -Default $DefaultOllamaPort
    $baseUrl = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_EMBEDDING_BASE_URL' -Default $DefaultEmbeddingBaseUrl
    $model = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_EMBEDDING_MODEL' -Default $DefaultEmbeddingModel
    $dimensions = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_EMBEDDING_DIMENSIONS' -Default ([string]$DefaultEmbeddingDimensions)
    $documentPrefix = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_EMBEDDING_DOCUMENT_PREFIX' -Default $DefaultDocumentPrefix -AllowEmpty
    $apiKey = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_EMBEDDING_API_KEY' -Default ''

    $baseUrlText = $baseUrl.Value.Trim().TrimEnd('/')
    $baseUri = $null
    if (-not [System.Uri]::TryCreate($baseUrlText, [System.UriKind]::Absolute, [ref]$baseUri) -or ($baseUri.Scheme -ne 'http' -and $baseUri.Scheme -ne 'https')) {
        throw (New-StepFailure -Reason "CAMPUS_RAG_EMBEDDING_BASE_URL is '$(Get-RedactedText $baseUrlText)' (from $($baseUrl.Source)); it must be an http or https URL." -Fix "Set CAMPUS_RAG_EMBEDDING_BASE_URL=$DefaultEmbeddingBaseUrl in .env")
    }
    $dimensionsNumber = 0
    if (-not [int]::TryParse($dimensions.Value, [ref]$dimensionsNumber) -or $dimensionsNumber -lt 1) {
        throw (New-StepFailure -Reason "CAMPUS_RAG_EMBEDDING_DIMENSIONS is '$($dimensions.Value)' (from $($dimensions.Source)); it must be a positive whole number." -Fix "Set CAMPUS_RAG_EMBEDDING_DIMENSIONS=$DefaultEmbeddingDimensions in .env (the size $DefaultEmbeddingModel returns)")
    }
    $displayBaseUrl = Get-RedactedText $baseUrlText
    if ($baseUri.IsLoopback -and $baseUri.Port -ne $ollamaPortNumber) {
        Write-Note "CAMPUS_RAG_EMBEDDING_BASE_URL uses port $($baseUri.Port), but the Ollama container publishes port $ollamaPortNumber (CAMPUS_RAG_OLLAMA_PORT). Make them match."
    }

    $script:ModelConfig = New-Object PSObject -Property @{
        OllamaPort     = $ollamaPortNumber
        BaseUrl        = $baseUrlText
        DisplayBaseUrl = $displayBaseUrl
        IsLocal        = $baseUri.IsLoopback
        Model          = $model.Value
        Dimensions     = $dimensionsNumber
        DocumentPrefix = $documentPrefix.Value
        ApiKey         = $apiKey.Value
    }
    Write-Detail "Model server port: $ollamaPortNumber (from $($ollamaPort.Source))"
    Write-Detail "Embeddings: $displayBaseUrl, model $($model.Value), $dimensionsNumber dimensions"

    $chatBaseUrl = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_BASE_URL' -Default $DefaultChatBaseUrl
    $answerModel = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_ANSWER_MODEL' -Default $DefaultAnswerModel
    $rewriteModel = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_REWRITE_MODEL' -Default $DefaultRewriteModel
    $judgeModel = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_JUDGE_MODEL' -Default $DefaultJudgeModel
    $chatTimeout = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_TIMEOUT_SECONDS' -Default ([string]$DefaultChatTimeoutSeconds)
    $chatApiKey = Get-ConfigValue -DotEnv $dotEnv -Name 'CAMPUS_RAG_CHAT_API_KEY' -Default ''

    $chatBaseUrlText = $chatBaseUrl.Value.Trim().TrimEnd('/')
    $chatUri = $null
    if (-not [System.Uri]::TryCreate($chatBaseUrlText, [System.UriKind]::Absolute, [ref]$chatUri) -or ($chatUri.Scheme -ne 'http' -and $chatUri.Scheme -ne 'https')) {
        throw (New-StepFailure -Reason "CAMPUS_RAG_CHAT_BASE_URL is '$(Get-RedactedText $chatBaseUrlText)' (from $($chatBaseUrl.Source)); it must be an http or https URL." -Fix "Set CAMPUS_RAG_CHAT_BASE_URL=$DefaultChatBaseUrl in .env")
    }
    $timeoutSeconds = 0.0
    if (-not [double]::TryParse($chatTimeout.Value, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$timeoutSeconds) -or $timeoutSeconds -le 0) {
        throw (New-StepFailure -Reason "CAMPUS_RAG_CHAT_TIMEOUT_SECONDS is '$($chatTimeout.Value)' (from $($chatTimeout.Source)); it must be a positive number." -Fix "Set CAMPUS_RAG_CHAT_TIMEOUT_SECONDS=$DefaultChatTimeoutSeconds in .env")
    }
    $chatDisplayBaseUrl = Get-RedactedText $chatBaseUrlText
    if ($chatUri.IsLoopback -and $chatUri.Port -ne $ollamaPortNumber) {
        Write-Note "CAMPUS_RAG_CHAT_BASE_URL uses port $($chatUri.Port), but the Ollama container publishes port $ollamaPortNumber (CAMPUS_RAG_OLLAMA_PORT). Make them match."
    }
    $script:ChatConfig = New-Object PSObject -Property @{
        BaseUrl        = $chatBaseUrlText
        DisplayBaseUrl = $chatDisplayBaseUrl
        IsLocal        = $chatUri.IsLoopback
        AnswerModel    = $answerModel.Value
        RewriteModel   = $rewriteModel.Value
        JudgeModel     = $judgeModel.Value
        TimeoutSeconds = [int][Math]::Ceiling($timeoutSeconds)
        ApiKey         = $chatApiKey.Value
    }
    Write-Detail "Chat: $chatDisplayBaseUrl, answer $($answerModel.Value), rewrite $($rewriteModel.Value), judge $($judgeModel.Value)"
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

function Assert-ServicePortFree {
    # Fails when the compose service is not running and another process already listens on its port.
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$PortFix
    )
    $running = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'ps', '--status', 'running', '-q', $Service) -Quiet
    if ($running.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "docker compose ps failed with exit code $($running.ExitCode): $(Get-LastLine $running.Output)" -Fix 'docker compose ps   (read its error, then rerun)')
    }
    if (Get-LastLine $running.Output) {
        return
    }
    $listeners = @(Get-PortListener -Port $Port)
    if ($listeners.Count -gt 0) {
        $owners = ($listeners | ForEach-Object { "$($_.ProcessName) (PID $($_.ProcessId))" }) -join ', '
        $fix = "Stop that process, or $PortFix, then rerun."
        if ($owners -match 'docker|wslrelay|vpnkit') {
            $fix = "Another container publishes port $Port; find it with: docker ps --filter publish=$Port   Stop it, or $PortFix, then rerun."
        }
        throw (New-StepFailure -Reason "port $Port is already in use by $owners." -Fix $fix)
    }
}

function Start-ComposeService {
    # Runs 'docker compose up -d --wait <service>' and turns failures into step failures.
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$PortFix,
        [Parameter(Mandatory = $true)][string]$HealthFix
    )
    $up = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'up', '-d', '--wait', $Service)
    if ($up.ExitCode -ne 0) {
        $output = $up.Output -join "`n"
        if ($output -match 'port is already allocated|address already in use|bind') {
            throw (New-StepFailure -Reason "the $Description could not bind port $Port; another process or container is using it." -Fix "$PortFix, then rerun. Find containers with: docker ps --filter publish=$Port")
        }
        if ($output -match 'unhealthy') {
            throw (New-StepFailure -Reason "the $Description container started but failed its health check." -Fix $HealthFix)
        }
        throw (New-StepFailure -Reason "docker compose up -d --wait $Service failed with exit code $($up.ExitCode): $(Get-LastLine $up.Output)" -Fix "Check network access for the image pull and read the error above. Inspect with: docker compose logs $Service")
    }
}

function Remove-DatabaseData {
    # Removes only the db container and its named volume. 'docker compose down --volumes' would also
    # delete the Ollama model volume.
    $config = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'config', '--format', 'json') -Quiet
    if ($config.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "docker compose config failed with exit code $($config.ExitCode): $(Get-LastLine $config.Output)" -Fix 'docker compose config --quiet   (read its error, then rerun)')
    }
    $volumeName = $null
    try {
        $volumeName = ($config.Output -join "`n" | ConvertFrom-Json).volumes.'campus-rag-db'.name
    }
    catch {
        $volumeName = $null
    }
    if (-not $volumeName) {
        throw (New-StepFailure -Reason 'could not find the campus-rag-db volume in compose.yaml.' -Fix 'git checkout -- compose.yaml   (then rerun)')
    }

    $remove = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'rm', '--stop', '--force', '-v', 'db')
    if ($remove.ExitCode -ne 0) {
        throw (New-StepFailure -Reason "docker compose rm db failed with exit code $($remove.ExitCode): $(Get-LastLine $remove.Output)" -Fix 'docker compose rm --stop --force db   (read its error, then rerun)')
    }
    $inspect = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('volume', 'inspect', $volumeName) -Quiet
    if ($inspect.ExitCode -eq 0) {
        $volume = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('volume', 'rm', $volumeName)
        if ($volume.ExitCode -ne 0) {
            throw (New-StepFailure -Reason "docker volume rm $volumeName failed with exit code $($volume.ExitCode): $(Get-LastLine $volume.Output)" -Fix "docker volume rm $volumeName   (read its error, then rerun)")
        }
    }
    Write-Detail "Local database container and volume $volumeName deleted. The model volume is kept."
}

function Invoke-Step5DatabaseContainer {
    $port = $script:DbConfig.Port
    if ($ResetDatabase) {
        Write-Host '    WARNING: -ResetDatabase deletes the local database container and ALL of its data.' -ForegroundColor Yellow
        Remove-DatabaseData
    }

    $portFix = 'set CAMPUS_RAG_DB_PORT to a free port (for example 5434) in .env'
    Assert-ServicePortFree -Service 'db' -Port $port -PortFix $portFix
    Start-ComposeService -Service 'db' -Description 'database' -Port $port -PortFix ($portFix.Substring(0, 1).ToUpper() + $portFix.Substring(1)) -HealthFix 'docker compose logs db   (read the error; if the data volume is corrupt, rerun with -ResetDatabase)'
    Write-Detail "Database is healthy on 127.0.0.1:$port."
}

function Test-ModelListed {
    # True when 'ollama list' output contains the model; a name without a tag matches ':latest'.
    param(
        [string[]]$ListOutput,
        [Parameter(Mandatory = $true)][string]$Model
    )
    $wanted = @($Model.ToLowerInvariant())
    if ($Model -notmatch ':') {
        $wanted += "$($Model.ToLowerInvariant()):latest"
    }
    foreach ($line in $ListOutput) {
        $name = ($line.Trim() -split '\s+')[0]
        if ($name -and $wanted -contains $name.ToLowerInvariant()) {
            return $true
        }
    }
    return $false
}

function Invoke-OllamaPull {
    # Pulls a model through the Ollama HTTP API and prints ASCII progress every 10 percent.
    # Returns $null on success or the error text.
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Model
    )
    $request = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/api/pull")
    $request.Method = 'POST'
    $request.ContentType = 'application/json'
    $request.Proxy = $null
    $request.Timeout = 60000
    $request.ReadWriteTimeout = 600000
    $body = [System.Text.Encoding]::UTF8.GetBytes((@{ model = $Model; stream = $true } | ConvertTo-Json -Compress))
    $response = $null
    try {
        $requestStream = $request.GetRequestStream()
        $requestStream.Write($body, 0, $body.Length)
        $requestStream.Close()
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $lastStatus = ''
        $lastDecile = @{}
        $succeeded = $false
        while ($null -ne ($line = $reader.ReadLine())) {
            if (-not $line.Trim()) {
                continue
            }
            $update = $line | ConvertFrom-Json
            if ($update.PSObject.Properties['error'] -and $update.error) {
                return [string]$update.error
            }
            $status = [string]$update.status
            if ($status -eq 'success') {
                $succeeded = $true
            }
            $isLayer = $status -match '^pulling [0-9a-f]{12}'
            if ($isLayer) {
                if ($update.PSObject.Properties['total'] -and $update.PSObject.Properties['completed'] -and $update.total -ge 1000000) {
                    $layer = [string]$update.digest
                    $decile = [int][Math]::Floor(10 * $update.completed / $update.total)
                    if (-not $lastDecile.ContainsKey($layer) -or $decile -gt $lastDecile[$layer]) {
                        $lastDecile[$layer] = $decile
                        Write-Detail ('{0}: {1,3}% of {2:N0} MB' -f $status, (10 * $decile), ($update.total / 1000000))
                    }
                }
            }
            elseif ($status -ne $lastStatus) {
                Write-Detail $status
            }
            $lastStatus = $status
        }
        if (-not $succeeded) {
            return 'the pull ended without reporting success'
        }
        return $null
    }
    catch {
        $message = $_.Exception.Message
        if ($_.Exception.InnerException) {
            $message = $_.Exception.InnerException.Message
        }
        return $message
    }
    finally {
        if ($response) {
            $response.Close()
        }
    }
}

function Invoke-JsonPost {
    # POSTs JSON to an OpenAI-compatible endpoint and returns the parsed response. Failures become
    # step failures that name the endpoint without credentials. The API key is never printed.
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$DisplayUrl,
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][hashtable]$Payload,
        [string]$ApiKey,
        [bool]$IsLocal,
        [int]$TimeoutSeconds = 120,
        [Parameter(Mandatory = $true)][string]$ReachFix,
        [string]$TimeoutFix
    )
    $headers = @{}
    if ($ApiKey) {
        $headers['Authorization'] = 'Bearer ' + $ApiKey
    }
    $request = @{
        Uri         = $Url
        Method      = 'Post'
        ContentType = 'application/json'
        Body        = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Compress -Depth 5))
        Headers     = $headers
        TimeoutSec  = $TimeoutSeconds
        ErrorAction = 'Stop'
    }
    if ($IsLocal -and $PSVersionTable.PSVersion.Major -ge 7) {
        $request['NoProxy'] = $true
    }
    try {
        return Invoke-RestMethod @request
    }
    catch {
        $status = $null
        $responseProperty = $_.Exception.PSObject.Properties['Response']
        if ($responseProperty -and $responseProperty.Value) {
            try {
                $status = [int]$responseProperty.Value.StatusCode
            }
            catch {
                $status = $null
            }
        }
        $detail = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $detail = $_.ErrorDetails.Message
        }
        $detail = Get-RedactedText ($detail -replace '\s+', ' ').Trim()
        if ($detail.Length -gt 300) {
            $detail = $detail.Substring(0, 300) + '...'
        }
        if ($detail -match '(?i)more system memory') {
            throw (New-StepFailure -Reason "the $Kind endpoint $DisplayUrl reports that the model needs more memory than Docker has: $detail" -Fix 'Give Docker more memory (Docker Desktop Settings > Resources, or memory= in %UserProfile%\.wslconfig for the WSL 2 backend), then rerun')
        }
        if ($status) {
            throw (New-StepFailure -Reason "the $Kind endpoint $DisplayUrl returned HTTP ${status}: $detail" -Fix $ReachFix)
        }
        if ($TimeoutFix -and $detail -match '(?i)timed out|timeout') {
            throw (New-StepFailure -Reason "the $Kind endpoint $DisplayUrl did not respond within $TimeoutSeconds seconds." -Fix $TimeoutFix)
        }
        throw (New-StepFailure -Reason "cannot reach the $Kind endpoint $DisplayUrl ($detail)." -Fix $ReachFix)
    }
}

function Test-EmbeddingEndpoint {
    # Embeds one prefixed sample text and checks the vector size.
    $config = $script:ModelConfig
    $displayUrl = "$($config.DisplayBaseUrl)/embeddings"
    $sample = "$($config.DocumentPrefix)The campus library is open until midnight during finals week."
    $reachFix = 'docker compose ps ollama   (it should show "healthy"); make sure the port in CAMPUS_RAG_EMBEDDING_BASE_URL matches CAMPUS_RAG_OLLAMA_PORT in .env, then rerun'
    if (-not $config.IsLocal) {
        $reachFix = 'Check CAMPUS_RAG_EMBEDDING_BASE_URL, CAMPUS_RAG_EMBEDDING_MODEL, and CAMPUS_RAG_EMBEDDING_API_KEY in .env, then rerun'
    }
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-JsonPost -Url "$($config.BaseUrl)/embeddings" -DisplayUrl $displayUrl -Kind 'embeddings' -Payload @{ model = $config.Model; input = @($sample) } -ApiKey $config.ApiKey -IsLocal $config.IsLocal -TimeoutSeconds 120 -ReachFix $reachFix

    $vector = $null
    if ($response -and $response.PSObject.Properties['data'] -and @($response.data).Count -gt 0) {
        $first = @($response.data)[0]
        if ($first.PSObject.Properties['embedding']) {
            $vector = @($first.embedding)
        }
    }
    if ($null -eq $vector -or $vector.Count -eq 0) {
        throw (New-StepFailure -Reason "the embeddings endpoint $displayUrl returned no embedding." -Fix $reachFix)
    }
    if ($vector.Count -ne $config.Dimensions) {
        throw (New-StepFailure -Reason "model $($config.Model) returns $($vector.Count)-dimensional vectors, but CAMPUS_RAG_EMBEDDING_DIMENSIONS is $($config.Dimensions)." -Fix "Set CAMPUS_RAG_EMBEDDING_DIMENSIONS=$($vector.Count) in .env (and remove any CAMPUS_RAG_EMBEDDING_DIMENSIONS environment variable), or choose a model that returns $($config.Dimensions) dimensions")
    }
    Write-Detail ('Embedding check passed: {0} returned {1} dimensions in {2:N1}s.' -f $config.Model, $vector.Count, $timer.Elapsed.TotalSeconds)
}

function Test-ChatEndpoint {
    # Requests a tiny chat completion from the answer model.
    $chat = $script:ChatConfig
    $displayUrl = "$($chat.DisplayBaseUrl)/chat/completions"
    $reachFix = 'docker compose ps ollama   (it should show "healthy"); make sure the port in CAMPUS_RAG_CHAT_BASE_URL matches CAMPUS_RAG_OLLAMA_PORT in .env, then rerun'
    if (-not $chat.IsLocal) {
        $reachFix = 'Check CAMPUS_RAG_CHAT_BASE_URL, CAMPUS_RAG_CHAT_ANSWER_MODEL, and CAMPUS_RAG_CHAT_API_KEY in .env, then rerun'
    }
    $timeoutFix = "Raise CAMPUS_RAG_CHAT_TIMEOUT_SECONDS in .env (now $($chat.TimeoutSeconds)), or give Docker more CPU and memory, then rerun"
    $payload = @{
        model       = $chat.AnswerModel
        messages    = @(@{ role = 'user'; content = 'Reply with the single word OK.' })
        max_tokens  = 5
        temperature = 0
    }
    Write-Detail "Checking chat completions with $($chat.AnswerModel) (loading the model can take a minute)..."
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-JsonPost -Url "$($chat.BaseUrl)/chat/completions" -DisplayUrl $displayUrl -Kind 'chat' -Payload $payload -ApiKey $chat.ApiKey -IsLocal $chat.IsLocal -TimeoutSeconds $chat.TimeoutSeconds -ReachFix $reachFix -TimeoutFix $timeoutFix

    $content = $null
    if ($response -and $response.PSObject.Properties['choices'] -and @($response.choices).Count -gt 0) {
        $choice = @($response.choices)[0]
        if ($choice.PSObject.Properties['message'] -and $choice.message -and $choice.message.PSObject.Properties['content']) {
            $content = [string]$choice.message.content
        }
    }
    if ($null -eq $content) {
        throw (New-StepFailure -Reason "the chat endpoint $displayUrl returned no message." -Fix $reachFix)
    }
    $reply = (($content -replace '[^\x20-\x7E]', ' ') -replace '\s+', ' ').Trim()
    if ($reply.Length -gt 40) {
        $reply = $reply.Substring(0, 40) + '...'
    }
    Write-Detail ('Chat check passed: {0} replied "{1}" in {2:N1}s.' -f $chat.AnswerModel, $reply, $timer.Elapsed.TotalSeconds)
}

function Add-ModelRequirement {
    # Adds a model to the pull list, merging duplicates ('name' and 'name:latest' are the same model).
    param(
        [Parameter(Mandatory = $true)]$List,
        [Parameter(Mandatory = $true)][string]$Model,
        [Parameter(Mandatory = $true)][string]$Setting
    )
    $key = $Model.ToLowerInvariant()
    if ($key -notmatch ':') {
        $key = "${key}:latest"
    }
    foreach ($entry in $List) {
        if ($entry.Key -eq $key) {
            $entry.Settings = @($entry.Settings) + $Setting
            return
        }
    }
    $List.Add((New-Object PSObject -Property @{ Key = $key; Model = $Model; Settings = @($Setting) }))
}

function Invoke-Step6ModelServer {
    if ($SkipModelServer) {
        Write-Detail 'Skipped (-SkipModelServer). Commands that embed text or call chat models fail until you rerun without -SkipModelServer.'
        return
    }
    $config = $script:ModelConfig
    $chat = $script:ChatConfig
    $checkChat = -not $SkipChatModels

    $required = New-Object System.Collections.Generic.List[object]
    if ($config.IsLocal) {
        Add-ModelRequirement -List $required -Model $config.Model -Setting 'CAMPUS_RAG_EMBEDDING_MODEL'
    }
    if ($checkChat -and $chat.IsLocal) {
        Add-ModelRequirement -List $required -Model $chat.AnswerModel -Setting 'CAMPUS_RAG_CHAT_ANSWER_MODEL'
        Add-ModelRequirement -List $required -Model $chat.RewriteModel -Setting 'CAMPUS_RAG_CHAT_REWRITE_MODEL'
        Add-ModelRequirement -List $required -Model $chat.JudgeModel -Setting 'CAMPUS_RAG_CHAT_JUDGE_MODEL'
    }

    if ($required.Count -gt 0) {
        $port = $config.OllamaPort
        $portFix = 'set CAMPUS_RAG_OLLAMA_PORT to a free port (for example 11436) in .env and use the same port in CAMPUS_RAG_EMBEDDING_BASE_URL and CAMPUS_RAG_CHAT_BASE_URL'
        Assert-ServicePortFree -Service 'ollama' -Port $port -PortFix $portFix
        Start-ComposeService -Service 'ollama' -Description 'model server' -Port $port -PortFix ($portFix.Substring(0, 1).ToUpper() + $portFix.Substring(1)) -HealthFix 'docker compose logs ollama   (read the error, then rerun)'
        Write-Detail "Model server is healthy on 127.0.0.1:$port."

        $list = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'exec', '-T', 'ollama', 'ollama', 'list') -Quiet
        if ($list.ExitCode -ne 0) {
            throw (New-StepFailure -Reason "'ollama list' failed with exit code $($list.ExitCode): $(Get-LastLine $list.Output)" -Fix 'docker compose logs ollama   (read the error, then rerun)')
        }
        $present = @($required | Where-Object { Test-ModelListed -ListOutput $list.Output -Model $_.Model })
        $missing = @($required | Where-Object { -not (Test-ModelListed -ListOutput $list.Output -Model $_.Model) })
        if ($present.Count -gt 0) {
            Write-Detail "Already pulled: $(($present | ForEach-Object { $_.Model }) -join ', ')"
        }
        if ($missing.Count -gt 0) {
            $names = ($missing | ForEach-Object {
                    $size = $KnownModelSizes[($_.Key -replace ':latest$', '')]
                    if ($size) { "$($_.Model) ($size)" } else { $_.Model }
                }) -join ', '
            Write-Detail "Pulling $($missing.Count) missing model(s): $names"
            Write-Detail 'The default models total about 12 GB; the first download can take a while. Later runs reuse them.'
            foreach ($entry in $missing) {
                Write-Detail "Pulling model $($entry.Model)..."
                $pullError = Invoke-OllamaPull -Port $port -Model $entry.Model
                if ($pullError) {
                    $pullError = Get-RedactedText $pullError
                    $settings = (@($entry.Settings) | Sort-Object -Unique) -join ' and '
                    if ($pullError -match '(?i)file does not exist|not found|manifest unknown') {
                        throw (New-StepFailure -Reason "model $($entry.Model) was not found in the Ollama library ($pullError)." -Fix "Check $settings in .env, then rerun")
                    }
                    throw (New-StepFailure -Reason "could not pull model $($entry.Model): $pullError" -Fix "Check access to registry.ollama.ai and free disk space for Docker, then rerun. To pull by hand: docker compose exec ollama ollama pull $($entry.Model)")
                }
            }
        }
    }

    if (-not $config.IsLocal) {
        Write-Detail "CAMPUS_RAG_EMBEDDING_BASE_URL points to a remote provider ($($config.DisplayBaseUrl)); no embedding model is pulled."
    }
    Test-EmbeddingEndpoint

    if (-not $checkChat) {
        Write-Detail 'Chat models skipped (-SkipChatModels). The ask, evaluate, and api commands fail with exit code 7 until you rerun without -SkipChatModels.'
        return
    }
    if (-not $chat.IsLocal) {
        Write-Detail "CAMPUS_RAG_CHAT_BASE_URL points to a remote provider ($($chat.DisplayBaseUrl)); no chat models are pulled."
    }
    Test-ChatEndpoint
}

function Invoke-Step7Migrations {
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

function Invoke-Step8Verification {
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
    @{ Name = 'Model server'; Action = ${function:Invoke-Step6ModelServer} },
    @{ Name = 'Migrations'; Action = ${function:Invoke-Step7Migrations} },
    @{ Name = 'Verification'; Action = ${function:Invoke-Step8Verification} }
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
        Write-Host "  Database:   $($script:DbConfig.Target) (from $($script:DbConfig.TargetSource))"
        $model = $script:ModelConfig
        $embeddingText = "$($model.DisplayBaseUrl), model $($model.Model), $($model.Dimensions) dimensions"
        if ($SkipModelServer) {
            $embeddingText = "$embeddingText (not checked: -SkipModelServer)"
        }
        Write-Host "  Embeddings: $embeddingText"
        $chat = $script:ChatConfig
        $chatText = "$($chat.DisplayBaseUrl), answer $($chat.AnswerModel), rewrite $($chat.RewriteModel), judge $($chat.JudgeModel)"
        if ($SkipModelServer) {
            $chatText = "$chatText (not checked: -SkipModelServer)"
        }
        elseif ($SkipChatModels) {
            $chatText = "$chatText (not checked: -SkipChatModels)"
        }
        Write-Host "  Chat:       $chatText"
        Write-Host "  Python:     $pythonText"
        Write-Host "  Tests:      $testsText"
        Write-Host '  Stop:       docker compose stop db ollama'
        Write-Host '  Reset DB:   .\scripts\setup-local-environment.ps1 -ResetDatabase   (deletes database data; keeps models)'
        Write-Host "  Runbook:    $RunbookPath"
    }
}
finally {
    Pop-Location
}
exit $exitCode
