<#
.SYNOPSIS
Demonstrates the whole campus-rag system, from setup and ingestion to search,
cited answers, the HTTP API, the MCP server, and evaluation.

.DESCRIPTION
Runs a presenter-friendly tour of campus-rag. Each step prints a numbered
banner, a one-line "What this shows" talking point, and the exact command,
then runs the command with its output visible and prints the elapsed time.

Sections, in order:
  Setup       Verifies the local environment with
              .\scripts\setup-local-environment.ps1 -SkipTests.
  Ingest      Runs the offline pipeline on 5 pages without writing
              (select, polite fetch, extract, chunk), then summarizes the
              stored corpus. -FullIngest also runs the full ingestion.
  Search      The same question in keyword, vector, and hybrid search, then a
              policy number in keyword and vector search.
  Answers     A cited answer, a refusal, and a follow-up question that is
              rewritten into a standalone search query. (Slow: about a minute
              per answer on a CPU.)
  Api         Starts the HTTP API and the mock ticketing API if they are not
              running, then calls /health, /search, /answer, and /feedback and
              prints the usage report.
  Agent       Starts the MCP server over stdio as an AI agent does, lists its
              tools, searches, files a ticket, and reads the ticket back.
  Evaluation  Measures retrieval live against the golden set, shows the
              committed answer and embedding results, and answers one golden
              question live.

Before the first section that needs them, a preflight checks the repository,
uv, Docker, the db and ollama containers (starting them if needed), the
database schema, the corpus, and the models the selected sections use.

The script is safe to rerun. It never ingests the full corpus unless you pass
-FullIngest. It stops only the servers it started (by process id) and reuses
campus-rag servers that are already running. Server logs go to data\demo\ in
the repository, which Git ignores.

Exit codes:
   0  Success
   1  Unexpected error
   2  Invalid -Section value
  10  Preflight: the repository was not found
  11  Preflight: uv is not installed
  12  Preflight: Docker is not installed or its engine is not running
  13  Preflight: the db or ollama container is not healthy
  14  Preflight: the Python environment or migrations failed
  15  Preflight: the corpus is empty or not embedded
  16  Preflight: a required model is missing or the model server failed
  17  Preflight: port 8000 or 8001 is used by a program that is not campus-rag
  21  Section Setup failed
  22  Section Ingest failed
  23  Section Search failed
  24  Section Answers failed
  25  Section Api failed
  26  Section Agent failed
  27  Section Evaluation failed
  30  Cleanup: a server this script started did not stop

See runbooks/run-demo.md.

.PARAMETER Section
Which sections to run: All (default), Setup, Ingest, Search, Answers, Api,
Agent, or Evaluation. Pass several separated by commas, for example
-Section Search,Agent. Sections always run in the order listed above.

.PARAMETER Pause
Waits for Enter before each step so the presenter can talk. When standard
input is redirected (for example, piped), the script cannot read a key press
and does not pause.

.PARAMETER SkipSlow
Skips the steps that generate answers with the local LLM (about a minute
each): the Answers section, POST /answer in the Api section, and the live
answer evaluation. Use it for a short tour.

.PARAMETER KeepServers
Leaves the API and ticketing servers that this run started running at the
end, and prints how to stop them.

.PARAMETER ResetDemoData
At the end, deletes the request_log, answer_feedback, and mock_tickets rows
created during this run (rows with ids above the largest id recorded at the
start). Rows that existed before the run are kept. Rows that another program
writes while the demo runs are also deleted.

.PARAMETER FullIngest
In the Ingest section, also runs the full ingestion
(uv run python -m campus_rag.ingest): about 6 minutes of polite fetching plus
about 8 to 15 minutes to embed new chunks the first time. Off by default.

.EXAMPLE
.\scripts\demo.ps1

Runs every section, including the slow answer steps (about 8 minutes).

.EXAMPLE
.\scripts\demo.ps1 -Pause

Runs every section and waits for Enter before each step; use it when
presenting.

.EXAMPLE
.\scripts\demo.ps1 -SkipSlow

A quick tour of every section without generating answers (1 to 3 minutes).

.EXAMPLE
.\scripts\demo.ps1 -Section Agent

Shows only the MCP server: an AI agent's view of the tools, a search, and a
ticket.

.EXAMPLE
.\scripts\demo.ps1 -SkipSlow -ResetDemoData

A quick tour that removes the request log rows, ratings, and tickets it
created.

.NOTES
Runs under Windows PowerShell 5.1 and PowerShell 7, from any directory.
Requires the setup in runbooks/local-environment-setup.md and an ingested
corpus (runbooks/ingest-corpus.md).
#>
[CmdletBinding()]
param(
    [string[]]$Section = @('All'),
    [switch]$Pause,
    [switch]$SkipSlow,
    [switch]$KeepServers,
    [switch]$ResetDemoData,
    [switch]$FullIngest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunbookPath = 'runbooks\run-demo.md'
$LogDir = Join-Path $RepoRoot 'data\demo'
$ApiPort = 8000
$TicketPort = 8001
$ApiUrl = "http://127.0.0.1:$ApiPort"
$TicketUrl = "http://127.0.0.1:$TicketPort"
$ServerStartTimeoutSeconds = 90
$RepoUrl = 'https://github.com/eocrawford/campus-rag'

$SectionOrder = @('Setup', 'Ingest', 'Search', 'Answers', 'Api', 'Agent', 'Evaluation')
$SectionExitCodes = @{
    Setup = 21; Ingest = 22; Search = 23; Answers = 24; Api = 25; Agent = 26; Evaluation = 27
}

# Demo questions. They are written for this demo; each was checked against the indexed corpus.
$Questions = @{
    # Keyword search misses this paraphrase; vector search finds lawsuit and liability policies,
    # and hybrid search ranks the indemnification policy first.
    Paraphrase   = 'Who pays my legal bills if I get sued for something I did as part of my university job?'
    # A policy number: keyword search finds it; vector search returns neighboring numbers.
    ExactTerm    = 'APS 38.17'
    # Each answer below was checked against its cited passage in two live runs.
    # In both runs the model also cited a passage number it was not given, and code removed it.
    Answerable   = 'Is a service dog allowed inside a campus cafe?'
    Unanswerable = 'What time does the campus gym open on Sunday mornings?'
    ChatFirst    = 'What may university staff ask a person who brings a service animal into a building?'
    ChatFollowUp = 'Can they ask for proof that the dog is certified?'
    ApiSearch    = 'Does the university need my permission to show a movie in a lecture hall?'
    ApiAnswer    = 'Can my department give away old office furniture it no longer needs?'
    McpSearch    = 'Can I keep the old office chairs my department is throwing out?'
    McpTicket    = 'How do I reserve a group study room in the library during finals week?'
}
$LiveEvaluationId = 'q016'

$ScriptTimer = [System.Diagnostics.Stopwatch]::StartNew()
$script:StepNumber = 0
$script:Timings = New-Object System.Collections.Generic.List[object]
$script:Started = New-Object System.Collections.Generic.List[object]
$script:Reused = New-Object System.Collections.Generic.List[string]
$script:Python = $null
$script:UvExe = $null
$script:DockerExe = $null
$script:Baseline = $null
$script:CanPause = $false
$script:HttpClient = $null
$script:Warnings = New-Object System.Collections.Generic.List[string]

# --- output helpers ------------------------------------------------------------------------------

function New-DemoFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter(Mandatory = $true)][string]$Fix,
        [int]$ExitCode = 0
    )
    $exception = New-Object System.Exception $Reason
    $exception.Data['CampusRagFix'] = $Fix
    if ($ExitCode) {
        $exception.Data['CampusRagExitCode'] = $ExitCode
    }
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

function Write-Talk {
    param([string]$Message)
    Write-Host "What this shows: $Message" -ForegroundColor Green
}

function Format-Argument {
    # Quotes an argument for display the way it would be typed in PowerShell.
    param([string]$Value)
    if ($Value -match '[\s;''"&|<>(){}]' -or $Value -eq '') {
        return '"' + ($Value -replace '"', '`"') + '"'
    }
    return $Value
}

function Format-Command {
    param([string[]]$Parts)
    return (($Parts | ForEach-Object { Format-Argument $_ }) -join ' ')
}

function Format-Clip {
    # Flattens whitespace and clips text to a length, for previews.
    param([string]$Text, [int]$Limit = 160)
    if (-not $Text) {
        return ''
    }
    $flat = ($Text -replace '\s+', ' ').Trim()
    if ($flat.Length -le $Limit) {
        return $flat
    }
    return $flat.Substring(0, $Limit - 3).TrimEnd() + '...'
}

function Write-Wrapped {
    # Writes text wrapped to a width with an indent.
    param([string]$Text, [int]$Width = 100, [string]$Indent = '    ')
    $line = ''
    foreach ($word in (($Text -replace '\s+', ' ').Trim() -split ' ')) {
        if ($line -and ($Indent.Length + $line.Length + 1 + $word.Length) -gt $Width) {
            Write-Host "$Indent$line"
            $line = $word
        }
        elseif ($line) {
            $line = "$line $word"
        }
        else {
            $line = $word
        }
    }
    if ($line) {
        Write-Host "$Indent$line"
    }
}

function Write-SectionHeader {
    param([string]$Name, [string]$Summary)
    Write-Host ''
    Write-Host ('=' * 100) -ForegroundColor Cyan
    Write-Host ("  {0}: {1}  (t+{2:N0}s)" -f $Name.ToUpperInvariant(), $Summary, $ScriptTimer.Elapsed.TotalSeconds) -ForegroundColor Cyan
    Write-Host ('=' * 100) -ForegroundColor Cyan
}

function Wait-Presenter {
    if (-not $Pause -or -not $script:CanPause) {
        return
    }
    try {
        $null = Read-Host '(Press Enter to run this step; Ctrl+C stops the demo)'
    }
    catch {
        $script:CanPause = $false
        Write-Note 'This host cannot read input, so the demo continues without pausing.'
    }
}

function Invoke-DemoStep {
    # Prints a numbered banner, the talking point, and the command, waits for the presenter if
    # -Pause is set, runs the action, and records the elapsed time.
    param(
        [Parameter(Mandatory = $true)][string]$SectionName,
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Talk,
        [string[]]$Command = @(),
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $script:StepNumber++
    Write-Host ''
    Write-Host ("[{0}] {1}: {2}" -f $script:StepNumber, $SectionName, $Title) -ForegroundColor Cyan
    Write-Talk $Talk
    foreach ($line in $Command) {
        Write-Host "> $line" -ForegroundColor White
    }
    Wait-Presenter
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $status = 'OK'
    try {
        $null = & $Action
    }
    catch {
        $status = 'FAILED'
        throw
    }
    finally {
        $timer.Stop()
        $script:Timings.Add((New-Object PSObject -Property @{
                    Section = $SectionName; Step = $Title; Seconds = $timer.Elapsed.TotalSeconds; Status = $status
                }))
        $color = 'DarkGray'
        if ($status -ne 'OK') {
            $color = 'Red'
        }
        Write-Host ("[{0}] {1} in {2:N1}s" -f $script:StepNumber, $status, $timer.Elapsed.TotalSeconds) -ForegroundColor $color
    }
}

function Skip-DemoStep {
    param([string]$SectionName, [string]$Title, [string]$Reason)
    $script:StepNumber++
    Write-Host ''
    Write-Host ("[{0}] {1}: {2}  SKIPPED ({3})" -f $script:StepNumber, $SectionName, $Title, $Reason) -ForegroundColor DarkYellow
    $script:Timings.Add((New-Object PSObject -Property @{
                Section = $SectionName; Step = $Title; Seconds = 0.0; Status = 'SKIPPED'
            }))
}

# --- native commands -----------------------------------------------------------------------------

function Invoke-Native {
    # Runs a native command, echoes each output line (stdout and stderr) indented, and returns
    # the exit code and the lines. Lines from the HTTP client's request log are hidden.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$InputText,
        [switch]$Quiet
    )
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    $handle = {
        $line = $_.ToString()
        if ($line -eq 'System.Management.Automation.RemoteException') {
            # Windows PowerShell 5.1 renders an empty stderr line as the exception type name.
            $line = ''
        }
        $lines.Add($line)
        if (-not $Quiet -and $line -notmatch '^HTTP Request: ') {
            Write-Host "  $line"
        }
    }
    if ($PSBoundParameters.ContainsKey('InputText')) {
        $InputText | & $FilePath @ArgumentList 2>&1 | ForEach-Object $handle
    }
    else {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object $handle
    }
    $exitCode = $LASTEXITCODE
    return New-Object PSObject -Property @{ ExitCode = $exitCode; Output = $lines.ToArray() }
}

function Get-LastLine {
    param([string[]]$Lines)
    $nonEmpty = @($Lines | Where-Object { $_ -and $_.Trim() -and $_ -notmatch '^HTTP Request: ' })
    if ($nonEmpty.Count -eq 0) {
        return ''
    }
    return $nonEmpty[-1].Trim()
}

function Invoke-Uv {
    # Runs 'uv <arguments>' with visible output; throws a step failure on a nonzero exit code.
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$InputText,
        [string]$Fix = "Read the error above. Runbook: $RunbookPath"
    )
    if ($PSBoundParameters.ContainsKey('InputText')) {
        $result = Invoke-Native -FilePath $script:UvExe -ArgumentList $ArgumentList -InputText $InputText
    }
    else {
        $result = Invoke-Native -FilePath $script:UvExe -ArgumentList $ArgumentList
    }
    if ($result.ExitCode -ne 0) {
        throw (New-DemoFailure -Reason "'uv $($ArgumentList -join ' ')' exited with code $($result.ExitCode): $(Get-LastLine $result.Output)" -Fix $Fix)
    }
    return $result
}

function Invoke-PythonSnippet {
    # Runs a Python script (read from stdin) in the project environment and returns the JSON
    # object it prints on a line that starts with 'RESULT '.
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [string[]]$ArgumentList = @(),
        [switch]$Show
    )
    $python = $script:Python
    $arguments = @('-') + $ArgumentList
    if (-not $python) {
        $python = $script:UvExe
        $arguments = @('run', '--quiet', 'python', '-') + $ArgumentList
    }
    $result = Invoke-Native -FilePath $python -ArgumentList $arguments -InputText $Code -Quiet
    $json = $null
    foreach ($line in $result.Output) {
        if ($line.StartsWith('RESULT ')) {
            $json = $line.Substring(7) | ConvertFrom-Json
        }
        elseif ($Show -and $line -notmatch '^HTTP Request: ') {
            Write-Host "  $line"
        }
    }
    return New-Object PSObject -Property @{ ExitCode = $result.ExitCode; Result = $json; Output = $result.Output }
}

# --- HTTP ----------------------------------------------------------------------------------------

function Get-HttpClient {
    if (-not $script:HttpClient) {
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.UseProxy = $false
        $script:HttpClient = New-Object System.Net.Http.HttpClient $handler
        $script:HttpClient.Timeout = [TimeSpan]::FromSeconds(330)
    }
    return $script:HttpClient
}

function Invoke-Http {
    # Sends a request and returns the status code and the parsed JSON body. Never throws on an
    # HTTP error status; returns Status 0 when the server cannot be reached.
    param(
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [Parameter(Mandatory = $true)][string]$Url,
        $Body,
        [int]$TimeoutSeconds = 0
    )
    $client = Get-HttpClient
    $request = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::new($Method)), $Url
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 6 -Compress
        $request.Content = New-Object System.Net.Http.StringContent $json, ([System.Text.Encoding]::UTF8), 'application/json'
    }
    $cancel = New-Object System.Threading.CancellationTokenSource
    if ($TimeoutSeconds -gt 0) {
        $cancel.CancelAfter([TimeSpan]::FromSeconds($TimeoutSeconds))
    }
    try {
        $response = $client.SendAsync($request, $cancel.Token).GetAwaiter().GetResult()
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        $parsed = $null
        if ($text) {
            try {
                $parsed = $text | ConvertFrom-Json
            }
            catch {
                $parsed = $null
            }
        }
        return New-Object PSObject -Property @{ Status = [int]$response.StatusCode; Json = $parsed; Text = $text }
    }
    catch {
        return New-Object PSObject -Property @{ Status = 0; Json = $null; Text = $_.Exception.GetBaseException().Message }
    }
    finally {
        $request.Dispose()
        $cancel.Dispose()
    }
}

function Get-ErrorDetail {
    param($Response)
    if ($Response.Json -and ($Response.Json.PSObject.Properties.Name -contains 'detail')) {
        return [string]$Response.Json.detail
    }
    return (Format-Clip $Response.Text 200)
}

function Format-RestCommand {
    # The Invoke-RestMethod command equivalent to a request, for the audience to read or copy.
    param([string]$Method, [string]$Url, $Body)
    if ($null -eq $Body) {
        return "Invoke-RestMethod $Url"
    }
    $json = ($Body | ConvertTo-Json -Depth 6 -Compress) -replace "'", "''"
    return "Invoke-RestMethod -Method $Method $Url -ContentType application/json -Body '$json'"
}

# --- processes and ports -------------------------------------------------------------------------

function Get-PortListener {
    # Returns the ids of processes listening on a local TCP port.
    param([Parameter(Mandatory = $true)][int]$Port)
    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    return @($connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
}

function Get-ProcessLabel {
    param([int]$ProcessId)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) {
        return "process $ProcessId"
    }
    $commandLine = Format-Clip ([string]$process.CommandLine) 120
    return "process $ProcessId ($($process.Name)): $commandLine"
}

function Get-CampusRagServerKind {
    # Returns 'api' or 'ticketing' when a campus-rag server answers on the port, else $null.
    param([Parameter(Mandatory = $true)][int]$Port)
    $response = Invoke-Http -Url "http://127.0.0.1:$Port/openapi.json" -TimeoutSeconds 5
    if ($response.Status -ne 200 -or -not $response.Json) {
        return $null
    }
    $title = [string]$response.Json.info.title
    if ($title -eq 'campus-rag') {
        return 'api'
    }
    if ($title -eq 'campus-rag mock ticketing') {
        return 'ticketing'
    }
    return $null
}

function Get-DescendantProcessId {
    # The ids of all processes below a parent process, deepest first, that started after it.
    param([int]$ParentId, [datetime]$NotBefore)
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $result = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($ParentId)
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        foreach ($child in $all) {
            if ([int]$child.ParentProcessId -eq $current -and [int]$child.ProcessId -ne $current) {
                # A process that started before the parent only reuses an old parent id.
                if ($child.CreationDate -and $child.CreationDate -lt $NotBefore) {
                    continue
                }
                if (-not $result.Contains([int]$child.ProcessId)) {
                    $result.Add([int]$child.ProcessId)
                    $queue.Enqueue([int]$child.ProcessId)
                }
            }
        }
    }
    $result.Reverse()
    return $result.ToArray()
}

function Start-DemoServer {
    # Starts a campus-rag server (api or ticketing) unless one already answers on its port.
    # Returns $true when this run started it.
    param(
        [Parameter(Mandatory = $true)][ValidateSet('api', 'ticketing')][string]$Kind,
        [Parameter(Mandatory = $true)][int]$Port
    )
    $module = "campus_rag.$Kind"
    $own = @($script:Started | Where-Object { $_.Kind -eq $Kind -and -not $_.Process.HasExited })
    if ($own.Count -gt 0) {
        Write-Detail "The $Kind server this demo started is running on port $Port (process $($own[0].ProcessId))."
        return $false
    }
    if ($script:Reused.Contains($Kind)) {
        return $false
    }
    $listeners = @(Get-PortListener -Port $Port)
    if ($listeners.Count -gt 0) {
        $found = Get-CampusRagServerKind -Port $Port
        if ($found -eq $Kind) {
            Write-Detail "A campus-rag $Kind server is already running on port $Port (process $($listeners -join ', ')). Reusing it; this demo will leave it running."
            if (-not $script:Reused.Contains($Kind)) {
                $script:Reused.Add($Kind)
            }
            return $false
        }
        $owner = Get-ProcessLabel -ProcessId $listeners[0]
        throw (New-DemoFailure -Reason "port $Port is in use by $owner, which is not the campus-rag $Kind server." -Fix "Stop that program (Stop-Process -Id $($listeners[0]) if it is yours), then rerun." -ExitCode 17)
    }

    $null = New-Item -ItemType Directory -Path $LogDir -Force
    $stdout = Join-Path $LogDir "$Kind.stdout.log"
    $stderr = Join-Path $LogDir "$Kind.stderr.log"
    $startedAt = (Get-Date).AddSeconds(-1)
    $process = Start-Process -FilePath $script:Python -ArgumentList @('-m', $module, '--port', "$Port") -WorkingDirectory $RepoRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $script:Started.Add((New-Object PSObject -Property @{
                Kind = $Kind; Port = $Port; Process = $process; ProcessId = $process.Id; StartedAt = $startedAt; Children = @()
            }))
    Write-Detail "Started $Kind server: process $($process.Id), port $Port, log $stderr"

    $deadline = (Get-Date).AddSeconds($ServerStartTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            $tail = @(Get-Content -LiteralPath $stderr -Tail 5 -ErrorAction SilentlyContinue) -join ' | '
            throw (New-DemoFailure -Reason "the $Kind server exited with code $($process.ExitCode) before it answered: $tail" -Fix "Read $stderr. Reproduce with: uv run python -m $module")
        }
        $health = Invoke-Http -Url "http://127.0.0.1:$Port/health" -TimeoutSeconds 10
        if ($health.Status -ne 0) {
            $entry = $script:Started[$script:Started.Count - 1]
            $entry.Children = @(Get-DescendantProcessId -ParentId $process.Id -NotBefore $startedAt)
            Write-Detail ("The {0} server answered after {1:N1}s." -f $Kind, ((Get-Date) - $startedAt).TotalSeconds)
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    throw (New-DemoFailure -Reason "the $Kind server did not answer on port $Port within $ServerStartTimeoutSeconds seconds." -Fix "Read $stderr. Reproduce with: uv run python -m $module")
}

function Stop-StartedServer {
    # Stops the servers this run started, with their child processes, and checks their ports.
    # Returns $true when every port is free.
    $allFree = $true
    foreach ($entry in $script:Started) {
        $ids = New-Object System.Collections.Generic.List[int]
        foreach ($id in @(Get-DescendantProcessId -ParentId $entry.ProcessId -NotBefore $entry.StartedAt)) {
            $ids.Add($id)
        }
        foreach ($id in @($entry.Children)) {
            if (-not $ids.Contains([int]$id)) {
                $ids.Add([int]$id)
            }
        }
        $ids.Add([int]$entry.ProcessId)
        foreach ($id in $ids) {
            $process = Get-Process -Id $id -ErrorAction SilentlyContinue
            if ($process -and $process.StartTime -ge $entry.StartedAt) {
                Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
            }
        }
        $deadline = (Get-Date).AddSeconds(10)
        $listeners = @(Get-PortListener -Port $entry.Port)
        while ($listeners.Count -gt 0 -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 250
            $listeners = @(Get-PortListener -Port $entry.Port)
        }
        if ($listeners.Count -gt 0) {
            $allFree = $false
            Write-Host "    Port $($entry.Port) is still in use by $(Get-ProcessLabel -ProcessId $listeners[0])." -ForegroundColor Red
        }
        else {
            Write-Detail "Stopped the $($entry.Kind) server (process $($entry.ProcessId) and $($ids.Count - 1) child process(es)); port $($entry.Port) is free."
        }
    }
    return $allFree
}

# --- preflight -----------------------------------------------------------------------------------

function Get-Setting {
    # A setting from the process environment, else from the repository's .env file, else the default.
    param([Parameter(Mandatory = $true)][string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($value) {
        return $value
    }
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
                $found = $Matches[1].Trim('"', "'")
                if ($found) {
                    return $found
                }
            }
        }
    }
    return $Default
}
function Find-Docker {
    $docker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($docker) {
        return $docker.Source
    }
    $candidate = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
    if (Test-Path -LiteralPath $candidate) {
        $env:PATH = "$(Split-Path -Parent $candidate);$env:PATH"
        return $candidate
    }
    return $null
}

function Get-ComposeHealth {
    # Returns a hashtable of service name to health ('healthy', 'starting', 'stopped', ...).
    $result = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('compose', 'ps', '--all', '--format', 'json') -Quiet
    $health = @{}
    if ($result.ExitCode -ne 0) {
        return $health
    }
    foreach ($line in $result.Output) {
        $text = $line.Trim()
        if (-not $text.StartsWith('{') -and -not $text.StartsWith('[')) {
            continue
        }
        foreach ($item in @($text | ConvertFrom-Json)) {
            $state = [string]$item.State
            $value = $state
            if ($state -eq 'running' -and $item.PSObject.Properties.Name -contains 'Health' -and $item.Health) {
                $value = [string]$item.Health
            }
            $health[[string]$item.Service] = $value
        }
    }
    return $health
}

function Get-RequiredModelRole {
    param([string[]]$Sections)
    $roles = New-Object System.Collections.Generic.List[string]
    $needsEmbedding = $FullIngest -and ($Sections -contains 'Ingest')
    foreach ($name in @('Search', 'Answers', 'Api', 'Agent', 'Evaluation')) {
        if ($Sections -contains $name) {
            $needsEmbedding = $true
        }
    }
    if ($needsEmbedding) {
        $roles.Add('embedding')
    }
    $answers = (-not $SkipSlow) -and ($Sections -contains 'Answers')
    $evaluation = (-not $SkipSlow) -and ($Sections -contains 'Evaluation')
    if ($answers -or $evaluation -or ($Sections -contains 'Api')) {
        $roles.Add('answer')
    }
    if ($answers -or $evaluation) {
        $roles.Add('rewrite')
    }
    if ($evaluation) {
        $roles.Add('judge')
    }
    return $roles.ToArray()
}

$CorpusCheckCode = @'
import json
import psycopg
from campus_rag.config import load_settings

settings = load_settings()
with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
    def scalar(sql, *params):
        return conn.execute(sql, params).fetchone()[0]
    result = {
        "documents": scalar("SELECT count(*) FROM documents"),
        "chunks": scalar("SELECT count(*) FROM chunks"),
        "embedded": scalar(
            "SELECT count(*) FROM chunk_embeddings WHERE model = %s", settings.embedding.model
        ),
        "model": settings.embedding.model,
        "request_log": scalar("SELECT coalesce(max(id), 0) FROM request_log"),
        "answer_feedback": scalar("SELECT coalesce(max(id), 0) FROM answer_feedback"),
        "mock_tickets": scalar("SELECT coalesce(max(id), 0) FROM mock_tickets"),
    }
print("RESULT " + json.dumps(result))
'@

$ModelCheckCode = @'
import json
import sys
from campus_rag.config import load_settings
from campus_rag.generate.chat import ChatError, OpenAICompatibleChat
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder

settings = load_settings()
checks = []
with OpenAICompatibleEmbedder(settings.embedding) as embedder, OpenAICompatibleChat(settings.chat) as chat:
    for role in sys.argv[1:]:
        if role == "embedding":
            model = settings.embedding.model
            try:
                embedder.check()
                checks.append({"role": role, "model": model, "error": None})
            except EmbeddingError as exc:
                checks.append({"role": role, "model": model, "error": str(exc)})
        else:
            model = getattr(settings.chat, role + "_model")
            try:
                chat.check(model)
                checks.append({"role": role, "model": model, "error": None})
            except ChatError as exc:
                checks.append({"role": role, "model": model, "error": str(exc)})
print("RESULT " + json.dumps({"checks": checks}))
'@

function Invoke-Preflight {
    param([string[]]$Sections)
    Write-SectionHeader -Name 'Preflight' -Summary 'check that everything the demo needs is ready'

    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml')) -or -not (Test-Path -LiteralPath (Join-Path $RepoRoot 'src\campus_rag'))) {
        throw (New-DemoFailure -Reason "the campus-rag repository was not found at $RepoRoot." -Fix 'Run the script from a campus-rag clone: .\scripts\demo.ps1' -ExitCode 10)
    }
    Write-Detail "Repository: $RepoRoot"

    if (-not $script:UvExe) {
        throw (New-DemoFailure -Reason 'uv is not installed or not on PATH.' -Fix 'winget install --id astral-sh.uv -e   (then open a new terminal and run .\scripts\setup-local-environment.ps1)' -ExitCode 11)
    }
    Write-Detail "uv: $script:UvExe"

    $script:DockerExe = Find-Docker
    if (-not $script:DockerExe) {
        throw (New-DemoFailure -Reason 'Docker is not installed (docker is not on PATH and Docker Desktop was not found).' -Fix 'Install Docker Desktop, then run .\scripts\setup-local-environment.ps1' -ExitCode 12)
    }
    $info = Invoke-Native -FilePath $script:DockerExe -ArgumentList @('info', '--format', '{{.ServerVersion}}') -Quiet
    if ($info.ExitCode -ne 0) {
        throw (New-DemoFailure -Reason "the Docker engine is not running: $(Get-LastLine $info.Output)" -Fix 'Start Docker Desktop and wait until it says the engine is running, then rerun. (.\scripts\setup-local-environment.ps1 starts it for you.)' -ExitCode 12)
    }
    Write-Detail "Docker engine: $(Get-LastLine $info.Output)"

    $health = Get-ComposeHealth
    $toStart = New-Object System.Collections.Generic.List[string]
    if (-not ($health.ContainsKey('db') -and $health['db'] -eq 'healthy')) {
        $toStart.Add('db')
    }
    $ollamaText = $null
    if ($health.ContainsKey('ollama') -and $health['ollama'] -eq 'healthy') {
        $ollamaText = 'healthy'
    }
    else {
        $ollamaPort = 11434
        $null = [int]::TryParse((Get-Setting -Name 'CAMPUS_RAG_OLLAMA_PORT' -Default '11434'), [ref]$ollamaPort)
        $listeners = @(Get-PortListener -Port $ollamaPort)
        if ($listeners.Count -gt 0) {
            # Another Compose project or a local Ollama already serves the models; starting this
            # project's container would fail on the busy port. The model check below verifies it.
            $ollamaText = "not running in this Compose project; using the model server already on port $ollamaPort"
        }
        else {
            $toStart.Add('ollama')
        }
    }
    if ($toStart.Count -gt 0) {
        $services = $toStart -join ' '
        Write-Detail "Starting containers that are not ready: $services"
        Write-Host "> docker compose up -d --wait $services" -ForegroundColor White
        $up = Invoke-Native -FilePath $script:DockerExe -ArgumentList (@('compose', 'up', '-d', '--wait') + $toStart.ToArray())
        if ($up.ExitCode -ne 0) {
            throw (New-DemoFailure -Reason "docker compose up -d --wait $services failed with exit code $($up.ExitCode): $(Get-LastLine $up.Output)" -Fix "Run .\scripts\setup-local-environment.ps1 (it creates and checks the containers), or inspect: docker compose logs $services" -ExitCode 13)
        }
        $health = Get-ComposeHealth
    }
    if (-not $ollamaText) {
        $ollamaText = $health['ollama']
    }
    Write-Detail "Containers: db=$($health['db']), ollama=$ollamaText"

    $resolved = Invoke-Native -FilePath $script:UvExe -ArgumentList @('run', '--quiet', 'python', '-c', 'import sys; print(sys.executable)') -Quiet
    $pythonPath = Get-LastLine $resolved.Output
    if ($resolved.ExitCode -ne 0 -or -not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) {
        throw (New-DemoFailure -Reason "the project's Python environment is not usable: $pythonPath" -Fix '.\scripts\setup-local-environment.ps1' -ExitCode 14)
    }
    $script:Python = $pythonPath
    Write-Detail "Python: $pythonPath"

    $migrate = Invoke-Native -FilePath $script:Python -ArgumentList @('-m', 'campus_rag.index.migrate') -Quiet
    if ($migrate.ExitCode -ne 0) {
        throw (New-DemoFailure -Reason "migrations failed (exit code $($migrate.ExitCode)): $(Get-LastLine $migrate.Output)" -Fix 'Reproduce with: uv run python -m campus_rag.index.migrate   (or reset with .\scripts\setup-local-environment.ps1 -ResetDatabase)' -ExitCode 14)
    }
    Write-Detail "Schema: $(Get-LastLine $migrate.Output)"

    $corpus = Invoke-PythonSnippet -Code $CorpusCheckCode
    if ($corpus.ExitCode -ne 0 -or -not $corpus.Result) {
        throw (New-DemoFailure -Reason "the corpus check failed: $(Get-LastLine $corpus.Output)" -Fix 'docker compose ps   (db must be healthy), then rerun' -ExitCode 15)
    }
    $counts = $corpus.Result
    $script:Baseline = $counts
    $willIngest = $FullIngest -and ($Sections -contains 'Ingest')
    $queryNeedsCorpus = @($Sections | Where-Object { $_ -in @('Search', 'Answers', 'Api', 'Agent', 'Evaluation') }).Count -gt 0
    if ([int]$counts.documents -eq 0 -or [int]$counts.embedded -eq 0) {
        $message = "the corpus has $($counts.documents) documents, $($counts.chunks) chunks, and $($counts.embedded) embeddings from $($counts.model)."
        if ($willIngest) {
            Write-Note "$message The Ingest section builds it (-FullIngest)."
        }
        elseif ($queryNeedsCorpus) {
            throw (New-DemoFailure -Reason $message -Fix 'Ingest the corpus first (about 15 minutes): uv run python -m campus_rag.ingest   (see runbooks\ingest-corpus.md), or rerun with -Section Ingest -FullIngest' -ExitCode 15)
        }
        else {
            Write-Note $message
        }
    }
    else {
        Write-Detail "Corpus: $($counts.documents) documents, $($counts.chunks) chunks, $($counts.embedded) embedded with $($counts.model)"
        if ([int]$counts.embedded -lt [int]$counts.chunks) {
            Write-Note "$([int]$counts.chunks - [int]$counts.embedded) chunks have no embedding; search still works. Embed them with: uv run python -m campus_rag.ingest"
        }
    }
    Write-Detail "Demo data before this run: request_log max id $($counts.request_log), answer_feedback max id $($counts.answer_feedback), mock_tickets max id $($counts.mock_tickets)"

    $roles = @(Get-RequiredModelRole -Sections $Sections)
    if ($roles.Count -gt 0) {
        $models = Invoke-PythonSnippet -Code $ModelCheckCode -ArgumentList $roles
        if ($models.ExitCode -ne 0 -or -not $models.Result) {
            throw (New-DemoFailure -Reason "the model check failed: $(Get-LastLine $models.Output)" -Fix 'docker compose ps   (ollama must be healthy), then rerun' -ExitCode 16)
        }
        foreach ($check in @($models.Result.checks)) {
            if ($check.error) {
                throw (New-DemoFailure -Reason "the $($check.role) model '$($check.model)' is not available: $($check.error)" -Fix "docker compose exec ollama ollama pull $($check.model)   (or fix the CAMPUS_RAG_* model setting in .env or the environment)" -ExitCode 16)
            }
            Write-Detail ("Model ({0}): {1}" -f $check.role, $check.model)
        }
    }

    foreach ($port in @($ApiPort, $TicketPort)) {
        $needed = ($port -eq $ApiPort -and $Sections -contains 'Api') -or ($port -eq $TicketPort -and ($Sections -contains 'Api' -or $Sections -contains 'Agent'))
        if (-not $needed) {
            continue
        }
        $listeners = @(Get-PortListener -Port $port)
        if ($listeners.Count -eq 0) {
            Write-Detail "Port ${port}: free"
            continue
        }
        $kind = Get-CampusRagServerKind -Port $port
        $expected = 'ticketing'
        if ($port -eq $ApiPort) {
            $expected = 'api'
        }
        if ($kind -ne $expected) {
            $owner = Get-ProcessLabel -ProcessId $listeners[0]
            throw (New-DemoFailure -Reason "port $port is in use by $owner, which is not the campus-rag $expected server." -Fix "Stop that program (Stop-Process -Id $($listeners[0]) if it is yours), then rerun." -ExitCode 17)
        }
        Write-Detail "Port ${port}: a campus-rag $expected server is already running (process $($listeners -join ', ')); the demo will reuse it."
    }
    Write-Host '    Preflight OK' -ForegroundColor Green
}

# --- sections ------------------------------------------------------------------------------------

function Invoke-SetupSection {
    $name = 'Setup'
    Write-SectionHeader -Name $name -Summary 'one command sets up and verifies the local environment'
    Invoke-DemoStep -SectionName $name -Title 'Verify the local environment' `
        -Talk 'One idempotent script installs and checks everything: Python, PostgreSQL with pgvector, the Ollama models, and migrations. On a ready machine it changes nothing.' `
        -Command @('.\scripts\setup-local-environment.ps1 -SkipTests') -Action {
        $setup = Join-Path $RepoRoot 'scripts\setup-local-environment.ps1'
        $global:LASTEXITCODE = 0
        & $setup -SkipTests | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw (New-DemoFailure -Reason "the setup script failed at step $LASTEXITCODE." -Fix 'Follow the Fix line printed above, then rerun. Runbook: runbooks\local-environment-setup.md')
        }
    }
}

$CorpusSummaryCode = @'
import psycopg
from campus_rag.config import load_settings

with psycopg.connect(load_settings().database_url) as conn:
    def show(label, sql):
        for row in conn.execute(sql).fetchall():
            print(f"{label:<12}", *row)

    show("Documents", "SELECT source, count(*), max(fetched_at)::date FROM documents GROUP BY source")
    show("Chunks", "SELECT count(*) FROM chunks")
    show("Tokens", "SELECT min(token_count), percentile_disc(0.5) WITHIN GROUP "
                   "(ORDER BY token_count), max(token_count) FROM chunks")
    show("Embeddings", "SELECT model, dimensions, count(*) FROM chunk_embeddings "
                       "GROUP BY model, dimensions ORDER BY model")
'@

function Invoke-IngestSection {
    $name = 'Ingest'
    Write-SectionHeader -Name $name -Summary 'the offline pipeline: select, fetch, extract, chunk, embed'
    try {
        Invoke-DemoStep -SectionName $name -Title 'Ingest 5 pages without writing (dry run)' `
            -Talk 'Ingestion is deterministic code: it reads the allowlisted sitemap, fetches one page per second as a polite crawler that obeys robots.txt, extracts the text, and splits it at headings into chunks of about 400 tokens. Short index pages are skipped.' `
            -Command @('uv run python -m campus_rag.ingest --limit 5 --dry-run') -Action {
            $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.ingest', '--limit', '5', '--dry-run') -Fix 'The dry run needs internet access to policy.uw.edu. Check the connection, or skip it: -Section Search,Answers,Api,Agent,Evaluation'
        }
    }
    catch {
        # A network problem should not end a demo of a corpus that is already stored.
        $script:Warnings.Add("Ingest dry run failed: $($_.Exception.Message)")
        Write-Note "The dry run failed ($($_.Exception.Message)). It needs internet access to policy.uw.edu; the demo continues with the stored corpus."
    }

    if ($FullIngest) {
        Invoke-DemoStep -SectionName $name -Title 'Ingest and embed the full corpus' `
            -Talk 'The full run is idempotent: unchanged documents stay untouched and only new or changed chunks are embedded.' `
            -Command @('uv run python -m campus_rag.ingest') -Action {
            $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.ingest') -Fix 'Read the error above; see runbooks\ingest-corpus.md'
        }
    }

    Invoke-DemoStep -SectionName $name -Title 'Summarize the stored corpus' `
        -Talk 'Documents, chunks, and vectors all live in one PostgreSQL database with pgvector, so search, filters, and logs use plain SQL.' `
        -Command @('uv run python -   (reads this script from standard input)') -Action {
        foreach ($line in ($CorpusSummaryCode -split "`r?`n")) {
            Write-Host "    | $line" -ForegroundColor DarkGray
        }
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-') -InputText $CorpusSummaryCode
    }
    if (-not $FullIngest) {
        Write-Detail 'The full ingestion is: uv run python -m campus_rag.ingest   (about 6 minutes of fetching plus 8 to 15 minutes'
        Write-Detail 'to embed new chunks the first time). Rerun this demo with -FullIngest to include it.'
    }
}

function Invoke-SearchSection {
    $name = 'Search'
    Write-SectionHeader -Name $name -Summary 'keyword, vector, and hybrid retrieval side by side'
    $question = $Questions.Paraphrase
    Invoke-DemoStep -SectionName $name -Title 'Keyword search (BM25)' `
        -Talk 'Keyword search matches words. This question shares few words with the policy that answers it, so the top results miss.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'keyword', '-k', '3')) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'keyword', '-k', '3')
    }
    Invoke-DemoStep -SectionName $name -Title 'Vector search (embeddings)' `
        -Talk 'Vector search matches meaning: without sharing the words, all three results are about lawsuits against employees and liability coverage.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'vector', '-k', '3')) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'vector', '-k', '3')
    }
    Invoke-DemoStep -SectionName $name -Title 'Hybrid search (reciprocal rank fusion)' `
        -Talk 'Hybrid search fuses both ranked lists with reciprocal rank fusion; the keyword and vector rank columns show where each result came from. It scored best on the golden set: recall@5 of 1.00.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'hybrid', '-k', '3')) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.retrieve', $question, '--mode', 'hybrid', '-k', '3')
    }
    $code = $Questions.ExactTerm
    Invoke-DemoStep -SectionName $name -Title 'An exact policy number in keyword search' `
        -Talk 'For codes and names, exact words matter: keyword search finds the policy by its number.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.retrieve', $code, '--mode', 'keyword', '-k', '3')) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.retrieve', $code, '--mode', 'keyword', '-k', '3')
    }
    Invoke-DemoStep -SectionName $name -Title 'The same policy number in vector search' `
        -Talk 'Embeddings blur numbers into neighbors, which is why the MCP tool lets an agent choose keyword mode for exact terms.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.retrieve', $code, '--mode', 'vector', '-k', '3')) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.retrieve', $code, '--mode', 'vector', '-k', '3')
    }
}

function Invoke-AnswersSection {
    $name = 'Answers'
    Write-SectionHeader -Name $name -Summary 'grounded answers with citations, refusals, and follow-ups'
    if ($SkipSlow) {
        Skip-DemoStep -SectionName $name -Title 'Answer, refuse, and follow up' -Reason '-SkipSlow'
        return
    }
    Invoke-DemoStep -SectionName $name -Title 'A cited answer' `
        -Talk 'A local 7B model answers only from the retrieved passages and cites them. Code checks every citation: watch for a removed citation to a passage the model was never given. About a minute on a CPU.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.ask', $Questions.Answerable)) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.ask', $Questions.Answerable)
    }
    Invoke-DemoStep -SectionName $name -Title 'A question the policies do not answer' `
        -Talk 'When the passages do not support an answer, the system says so with a fixed refusal instead of guessing. On the golden set no unanswerable question was answered.' `
        -Command @(Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.ask', $Questions.Unanswerable)) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.ask', $Questions.Unanswerable)
    }
    $chatCommand = "'$($Questions.ChatFollowUp)' | " + (Format-Command @('uv', 'run', 'python', '-m', 'campus_rag.ask', '--chat', $Questions.ChatFirst))
    Invoke-DemoStep -SectionName $name -Title 'A follow-up question in a conversation' `
        -Talk 'A small model rewrites the follow-up into a standalone search query from the conversation; the "Search query:" line shows the rewrite. Two answers, about two minutes.' `
        -Command @($chatCommand) -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.ask', '--chat', $Questions.ChatFirst) -InputText $Questions.ChatFollowUp
    }
}

function Write-HealthResponse {
    param($Response)
    $body = $Response.Json
    Write-Detail "HTTP $($Response.Status): status=$($body.status), schema_version=$($body.schema_version)"
    foreach ($component in @('database', 'embedding', 'chat')) {
        $item = $body.$component
        Write-Detail ("  {0,-10} ok={1}  {2}" -f $component, $item.ok, $item.detail)
    }
}

function Write-SearchResponse {
    param($Response)
    $body = $Response.Json
    Write-Detail "HTTP $($Response.Status): request_id=$($body.request_id), $(@($body.results).Count) result(s) in $($body.latency_ms) ms"
    foreach ($item in @($body.results)) {
        $keyword = '-'
        $vector = '-'
        if ($null -ne $item.keyword_rank) { $keyword = $item.keyword_rank }
        if ($null -ne $item.vector_rank) { $vector = $item.vector_rank }
        Write-Detail ("{0}. score={1:N4} keyword_rank={2} vector_rank={3}" -f $item.rank, $item.score, $keyword, $vector)
        Write-Detail "   $(Format-Clip $item.heading_path 96)"
        Write-Detail "   $($item.url)"
        Write-Detail "   $(Format-Clip $item.content_preview 120)"
    }
}

function Write-AnswerResponse {
    param($Response)
    $body = $Response.Json
    Write-Detail "HTTP $($Response.Status): request_id=$($body.request_id), refused=$($body.refused), model=$($body.model)"
    Write-Wrapped -Text $body.answer -Indent '    | '
    foreach ($citation in @($body.citations)) {
        Write-Detail "[$($citation.number)] $(Format-Clip $citation.heading_path 90)"
        Write-Detail "    $($citation.url)"
    }
    $timings = @($body.timings_ms.PSObject.Properties | ForEach-Object { '{0} {1:N1}s' -f $_.Name, ($_.Value / 1000) }) -join ', '
    Write-Detail "Timings: $timings"
    if ($body.suggest_ticket) {
        Write-Detail "suggest_ticket=true; ticket_draft.summary: $($body.ticket_draft.summary)"
    }
}

function Assert-HttpOk {
    param($Response, [string]$What, [int[]]$Expected = @(200))
    if ($Expected -notcontains $Response.Status) {
        $detail = Get-ErrorDetail $Response
        if ($Response.Status -eq 0) {
            throw (New-DemoFailure -Reason "$What failed: $detail" -Fix "Check that the server is running; its log is in $LogDir")
        }
        throw (New-DemoFailure -Reason "$What returned HTTP $($Response.Status): $detail" -Fix "Follow the message above. Server log: $LogDir")
    }
}

function Invoke-ApiSection {
    $name = 'Api'
    Write-SectionHeader -Name $name -Summary 'the same services over HTTP, with request logging and ratings'
    Invoke-DemoStep -SectionName $name -Title 'Start the API and the mock ticketing API' `
        -Talk 'FastAPI serves search and answers with OpenAPI docs; a second loopback service stands in for an IT service management (ITSM) ticketing system. Both bind to 127.0.0.1 only.' `
        -Command @('uv run python -m campus_rag.api         (port 8000; docs at http://127.0.0.1:8000/docs)', 'uv run python -m campus_rag.ticketing   (port 8001)') -Action {
        $null = Start-DemoServer -Kind 'api' -Port $ApiPort
        $null = Start-DemoServer -Kind 'ticketing' -Port $TicketPort
    }

    Invoke-DemoStep -SectionName $name -Title 'GET /health' `
        -Talk 'Health checks the database schema and both models, so an operator sees what is wrong and how to fix it.' `
        -Command @(Format-RestCommand -Method 'GET' -Url "$ApiUrl/health") -Action {
        $response = Invoke-Http -Url "$ApiUrl/health" -TimeoutSeconds 30
        if ($response.Status -eq 0 -or -not $response.Json) {
            Assert-HttpOk -Response $response -What 'GET /health'
        }
        Write-HealthResponse $response
        if ($response.Status -ne 200) {
            throw (New-DemoFailure -Reason 'the API reports a degraded component (see above).' -Fix 'Follow the detail shown for the failing component, then rerun.')
        }
    }

    $searchBody = [ordered]@{ query = $Questions.ApiSearch; mode = 'hybrid'; k = 3 }
    $script:LastRequestId = $null
    Invoke-DemoStep -SectionName $name -Title 'POST /search' `
        -Talk 'The same hybrid search over HTTP. Every request is logged with its timings and a request_id, and nothing that identifies the caller.' `
        -Command @(Format-RestCommand -Method 'Post' -Url "$ApiUrl/search" -Body $searchBody) -Action {
        $response = Invoke-Http -Method 'POST' -Url "$ApiUrl/search" -Body $searchBody -TimeoutSeconds 60
        Assert-HttpOk -Response $response -What 'POST /search'
        Write-SearchResponse $response
        $script:LastRequestId = $response.Json.request_id
    }

    if ($SkipSlow) {
        Skip-DemoStep -SectionName $name -Title 'POST /answer' -Reason '-SkipSlow'
    }
    else {
        $answerBody = [ordered]@{ question = $Questions.ApiAnswer }
        Invoke-DemoStep -SectionName $name -Title 'POST /answer' `
            -Talk 'A cited answer as JSON: citations, the search query used, per-stage timings, and, for a refusal, a prefilled ticket draft. About a minute on a CPU.' `
            -Command @((Format-RestCommand -Method 'Post' -Url "$ApiUrl/answer" -Body $answerBody) + ' -TimeoutSec 300') -Action {
            $response = Invoke-Http -Method 'POST' -Url "$ApiUrl/answer" -Body $answerBody -TimeoutSeconds 320
            Assert-HttpOk -Response $response -What 'POST /answer'
            Write-AnswerResponse $response
            $script:LastRequestId = $response.Json.request_id
        }
    }

    if ($null -eq $script:LastRequestId) {
        Skip-DemoStep -SectionName $name -Title 'POST /feedback' -Reason 'the previous request was not logged'
    }
    else {
        $feedbackBody = [ordered]@{ request_id = [int]$script:LastRequestId; rating = 1; comment = 'Rated during the campus-rag demo.' }
        Invoke-DemoStep -SectionName $name -Title 'POST /feedback' `
            -Talk 'Users rate an answer by its request_id; ratings feed the usage report, a simple quality signal from real use.' `
            -Command @(Format-RestCommand -Method 'Post' -Url "$ApiUrl/feedback" -Body $feedbackBody) -Action {
            $response = Invoke-Http -Method 'POST' -Url "$ApiUrl/feedback" -Body $feedbackBody -TimeoutSeconds 30
            Assert-HttpOk -Response $response -What 'POST /feedback' -Expected @(201)
            Write-Detail "HTTP $($response.Status): feedback_id=$($response.Json.feedback_id), request_id=$($response.Json.request_id)"
        }
    }

    Invoke-DemoStep -SectionName $name -Title 'Usage report' `
        -Talk 'Analytics from the local request log: volume, p50 and p95 latency, refusal rate, errors, and ratings.' `
        -Command @('uv run python -m campus_rag.analytics --days 1') -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.analytics', '--days', '1')
    }
}

function Get-McpConfigSnippet {
    # The Copilot CLI MCP configuration from the API and MCP runbook, pointed at this clone.
    $runbook = Join-Path $RepoRoot 'runbooks\run-api-and-mcp.md'
    $text = Get-Content -LiteralPath $runbook -Raw
    $match = [regex]::Match($text, '(?s)```json\s*(\{\s*"mcpServers".*?)```')
    if (-not $match.Success) {
        return $null
    }
    $snippet = $match.Groups[1].Value.TrimEnd()
    $escaped = $RepoRoot.Replace('\', '\\')
    $pathMatch = [regex]::Match($snippet, '"--directory",\s*"([^"]*)"')
    if ($pathMatch.Success) {
        $snippet = $snippet.Replace($pathMatch.Groups[1].Value, $escaped)
    }
    return $snippet
}

function Invoke-AgentSection {
    $name = 'Agent'
    Write-SectionHeader -Name $name -Summary 'an AI agent uses the MCP server: tools, search, and a ticket with consent'
    $script:TicketId = $null
    $serverLog = Join-Path $LogDir 'mcp-server.log'
    $clientArgs = @('run', 'python', 'scripts/demo_mcp_client.py', '--question', $Questions.McpSearch, '--ticket', '--ticket-question', $Questions.McpTicket, '--server-log', $serverLog)
    Invoke-DemoStep -SectionName $name -Title 'Connect to the MCP server as an agent would' `
        -Talk 'The client starts the MCP server over stdio, reads the tool descriptions an agent sees, searches, and, only with the user''s consent, files a ticket for a question the policies do not cover.' `
        -Command @(Format-Command (@('uv', 'run', 'python', 'scripts/demo_mcp_client.py', '--question', $Questions.McpSearch, '--ticket', '--ticket-question', $Questions.McpTicket))) -Action {
        $null = Start-DemoServer -Kind 'ticketing' -Port $TicketPort
        $null = New-Item -ItemType Directory -Path $LogDir -Force
        $result = Invoke-Uv -ArgumentList $clientArgs -Fix "Read the error above. The MCP server's log is $serverLog"
        foreach ($line in $result.Output) {
            if ($line -match '^Ticket id: (\d+)') {
                $script:TicketId = [int]$Matches[1]
            }
        }
    }

    if ($null -eq $script:TicketId) {
        Skip-DemoStep -SectionName $name -Title 'Read the ticket' -Reason 'no ticket id was printed'
    }
    else {
        $url = "$TicketUrl/tickets/$($script:TicketId)"
        Invoke-DemoStep -SectionName $name -Title 'Read the ticket from the ticketing API' `
            -Talk 'The ticket landed in the IT service management (ITSM) stand-in over HTTP, holding only what the user typed.' `
            -Command @(Format-RestCommand -Method 'GET' -Url $url) -Action {
            $response = Invoke-Http -Url $url -TimeoutSeconds 30
            Assert-HttpOk -Response $response -What "GET /tickets/$($script:TicketId)"
            $ticket = $response.Json
            Write-Detail "HTTP $($response.Status): id=$($ticket.id), status=$($ticket.status), category=$($ticket.category)"
            Write-Detail "summary: $($ticket.summary)"
            Write-Detail "source_question: $($ticket.source_question)"
        }
    }

    Invoke-DemoStep -SectionName $name -Title 'Connect a real agent' `
        -Talk 'GitHub Copilot CLI, Claude Desktop, or any MCP client starts the same server from a few lines of configuration.' `
        -Command @('Add to %USERPROFILE%\.copilot\mcp-config.json (or run /mcp in Copilot CLI):') -Action {
        $snippet = Get-McpConfigSnippet
        if (-not $snippet) {
            Write-Note 'The configuration snippet was not found in runbooks\run-api-and-mcp.md.'
            return
        }
        foreach ($line in ($snippet -split "`r?`n")) {
            Write-Host "    $line"
        }
        Write-Detail 'Then ask the agent a policy question; it calls search_policies and cites the URLs.'
    }
}

function Write-ReportHeadline {
    $resultsDir = Join-Path $RepoRoot 'docs\results'
    foreach ($file in @('retrieval.md', 'answers.md')) {
        $path = Join-Path $resultsDir $file
        $lines = @(Get-Content -LiteralPath $path)
        $headline = $lines | Where-Object { $_.Trim() -and -not $_.StartsWith('#') } | Select-Object -First 1
        $generated = $lines | Where-Object { $_ -match '^Generated on ([0-9-]+) at commit `([0-9a-f]+)`' } | Select-Object -First 1
        Write-Host ''
        Write-Detail "docs\results\$file"
        Write-Wrapped -Text $headline -Indent '      '
        if ($generated -and $generated -match '^Generated on ([0-9-]+) at commit `([0-9a-f]+)`') {
            Write-Detail "  (recorded run on $($Matches[1]) at commit $($Matches[2]))"
        }
    }
    $path = Join-Path $resultsDir 'embedding-strategies.md'
    $lines = @(Get-Content -LiteralPath $path)
    $start = [array]::IndexOf($lines, '## What this shows')
    Write-Host ''
    Write-Detail 'docs\results\embedding-strategies.md (What this shows)'
    if ($start -ge 0) {
        for ($index = $start + 1; $index -lt $lines.Count -and -not $lines[$index].StartsWith('## '); $index++) {
            if ($lines[$index].Trim()) {
                Write-Wrapped -Text $lines[$index] -Indent '      '
            }
        }
    }
}

function Invoke-EvaluationSection {
    $name = 'Evaluation'
    Write-SectionHeader -Name $name -Summary 'measured quality against a 47-question golden set'
    Invoke-DemoStep -SectionName $name -Title 'Measure retrieval live' `
        -Talk 'Every retrieval change is measured: recall@k and MRR for each mode over the golden set, plus latency and HNSW index recall, in about 20 seconds.' `
        -Command @('uv run python -m campus_rag.evaluate retrieval') -Action {
        $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.evaluate', 'retrieval')
    }
    Invoke-DemoStep -SectionName $name -Title 'The recorded answer and embedding results' `
        -Talk 'The full answer evaluation takes about two hours on a CPU, so its committed report is shown: refusal accuracy, citation validity, and judge-graded faithfulness and relevance.' `
        -Command @('Get-Content docs\results\retrieval.md, docs\results\answers.md, docs\results\embedding-strategies.md   (headlines)') -Action {
        Write-ReportHeadline
    }
    if ($SkipSlow) {
        Skip-DemoStep -SectionName $name -Title "Evaluate one golden question live ($LiveEvaluationId)" -Reason '-SkipSlow'
    }
    else {
        Invoke-DemoStep -SectionName $name -Title "Evaluate one golden question live ($LiveEvaluationId)" `
            -Talk 'The same harness on one question: answer, check citations and retrieval, then a second LLM (a different model family) judges faithfulness and relevance. Two to seven minutes, mostly loading the judge model; Ctrl+C ends the demo and still cleans up.' `
            -Command @("uv run python -m campus_rag.evaluate answers --ids $LiveEvaluationId") -Action {
            $null = Invoke-Uv -ArgumentList @('run', 'python', '-m', 'campus_rag.evaluate', 'answers', '--ids', $LiveEvaluationId)
        }
    }
}

# --- demo data and summary -----------------------------------------------------------------------

$DemoRowsCode = @'
import json
import sys
import psycopg
from campus_rag.config import load_settings

delete = sys.argv[1] == "delete"
baseline = {"mock_tickets": int(sys.argv[2]), "answer_feedback": int(sys.argv[3]),
            "request_log": int(sys.argv[4])}
result = {}
with psycopg.connect(load_settings().database_url, connect_timeout=5) as conn:
    for table, after in baseline.items():
        if delete:
            result[table] = conn.execute(f"DELETE FROM {table} WHERE id > %s", (after,)).rowcount
        else:
            result[table] = conn.execute(
                f"SELECT count(*) FROM {table} WHERE id > %s", (after,)
            ).fetchone()[0]
print("RESULT " + json.dumps(result))
'@

function Invoke-DemoRowAction {
    param([ValidateSet('count', 'delete')][string]$Action)
    $base = $script:Baseline
    $snippet = Invoke-PythonSnippet -Code $DemoRowsCode -ArgumentList @($Action, "$($base.mock_tickets)", "$($base.answer_feedback)", "$($base.request_log)")
    if ($snippet.ExitCode -ne 0 -or -not $snippet.Result) {
        Write-Note "Could not $Action the demo rows: $(Get-LastLine $snippet.Output)"
        return $null
    }
    return $snippet.Result
}

function Write-Finale {
    param([string[]]$Sections)
    Write-SectionHeader -Name 'Finale' -Summary 'what the demo showed'
    $shown = @{
        Setup      = 'one-command, idempotent setup of PostgreSQL + pgvector and local Ollama models'
        Preflight  = 'checks of Docker, the database schema, the corpus, and the models'
        Ingest     = 'a polite, deterministic ingestion pipeline and the stored corpus'
        Search     = 'keyword, vector, and hybrid retrieval and when each one wins'
        Answers    = 'cited answers, refusals instead of guesses, and follow-up rewriting'
        Api        = 'the HTTP API, request logging, ratings, and the usage report'
        Agent      = 'the MCP server as an AI agent sees it, and a ticket filed with consent'
        Evaluation = 'live retrieval metrics and the recorded answer evaluation'
    }
    Write-Host ''
    Write-Host '  Section      Time     Shown'
    $rows = @($Sections | Where-Object { $_ -eq 'Setup' }) + @('Preflight') + @($Sections | Where-Object { $_ -ne 'Setup' })
    foreach ($sectionName in $rows) {
        $steps = @($script:Timings | Where-Object { $_.Section -eq $sectionName })
        if ($steps.Count -eq 0) {
            continue
        }
        $seconds = ($steps | Measure-Object -Property Seconds -Sum).Sum
        $failed = @($steps | Where-Object { $_.Status -eq 'FAILED' }).Count -gt 0
        $mark = ''
        if ($failed) {
            $mark = '  (FAILED)'
        }
        elseif (@($steps | Where-Object { $_.Status -ne 'SKIPPED' }).Count -eq 0) {
            $mark = '  (skipped)'
        }
        Write-Host ("  {0,-11} {1,6:N0}s   {2}{3}" -f $sectionName, $seconds, $shown[$sectionName], $mark)
    }
    Write-Host ("  {0,-11} {1,6:N0}s" -f 'Total', $ScriptTimer.Elapsed.TotalSeconds)
    foreach ($warning in $script:Warnings) {
        Write-Note $warning
    }

    Write-Host ''
    Write-Host '  Learn more'
    Write-Host "    Repository:    $RepoUrl"
    Write-Host "    Case study:    $RepoUrl/blob/main/docs/case-study.md"
    Write-Host "    Architecture:  $RepoUrl/blob/main/docs/architecture.md   (diagram: docs\images\system-overview.png)"
    Write-Host "    This demo:     $RunbookPath"
    if (Test-Path -LiteralPath $LogDir) {
        Write-Host "    Server logs:   $LogDir"
    }
}

# --- main ----------------------------------------------------------------------------------------

# Split comma-separated values too: 'powershell -File' passes "Search,Agent" as one string.
$selected = @($Section | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$validSections = @('All') + $SectionOrder
$unknown = @($selected | Where-Object { $validSections -notcontains $_ })
if ($unknown.Count -gt 0 -or $selected.Count -eq 0) {
    Write-Host "Unknown -Section value: $($unknown -join ', '). Use one or more of: $($validSections -join ', ')" -ForegroundColor Red
    exit 2
}
if ($selected -contains 'All') {
    $selected = $SectionOrder
}
$selected = @($SectionOrder | Where-Object { $selected -contains $_ })

$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($uvCommand) {
    $script:UvExe = $uvCommand.Source
}

$script:CanPause = $true
try {
    if ([Console]::IsInputRedirected) {
        $script:CanPause = $false
    }
}
catch {
    $script:CanPause = $false
}

# Python writes UTF-8 and the console decodes it as UTF-8, so policy text shows correctly.
$savedEnv = @{}
foreach ($name in @('PYTHONIOENCODING', 'PYTHONUNBUFFERED')) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$savedOutputEncoding = $null
try {
    $savedOutputEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
}
catch {
    $savedOutputEncoding = $null
}

$exitCode = 0
$cleanupFailed = $false
Push-Location -LiteralPath $RepoRoot
try {
    Write-Host "campus-rag demo ($RepoRoot)" -ForegroundColor Cyan
    Write-Host "Sections: $($selected -join ', ')$(if ($SkipSlow) { '   (-SkipSlow: no LLM answers)' })"
    if ($Pause -and -not $script:CanPause) {
        Write-Note 'Standard input is redirected, so -Pause cannot wait for Enter; the demo runs without pausing.'
    }

    $current = 'Preflight'
    try {
        if ($selected -contains 'Setup') {
            $current = 'Setup'
            if (-not $script:UvExe) {
                # The setup script reports a missing uv with its own fix.
                Write-Note 'uv is not on PATH; the setup script will say how to install it.'
            }
            Invoke-SetupSection
            $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($uvCommand) {
                $script:UvExe = $uvCommand.Source
            }
        }
        $rest = @($selected | Where-Object { $_ -ne 'Setup' })
        if ($rest.Count -gt 0) {
            $current = 'Preflight'
            $preflightTimer = [System.Diagnostics.Stopwatch]::StartNew()
            $preflightStatus = 'FAILED'
            try {
                Invoke-Preflight -Sections $rest
                $preflightStatus = 'OK'
            }
            finally {
                $script:Timings.Add((New-Object PSObject -Property @{
                            Section = 'Preflight'; Step = 'Preflight'; Seconds = $preflightTimer.Elapsed.TotalSeconds; Status = $preflightStatus
                        }))
            }
            foreach ($sectionName in $rest) {
                $current = $sectionName
                switch ($sectionName) {
                    'Ingest' { Invoke-IngestSection }
                    'Search' { Invoke-SearchSection }
                    'Answers' { Invoke-AnswersSection }
                    'Api' { Invoke-ApiSection }
                    'Agent' { Invoke-AgentSection }
                    'Evaluation' { Invoke-EvaluationSection }
                }
            }
        }
    }
    catch {
        $exception = $_.Exception
        $fix = $null
        if ($exception.Data.Contains('CampusRagFix')) {
            $fix = [string]$exception.Data['CampusRagFix']
        }
        if ($exception.Data.Contains('CampusRagExitCode')) {
            $exitCode = [int]$exception.Data['CampusRagExitCode']
        }
        elseif ($SectionExitCodes.ContainsKey($current)) {
            $exitCode = $SectionExitCodes[$current]
        }
        else {
            $exitCode = 1
        }
        Write-Host ''
        Write-Host "FAILED in $current (exit code $exitCode): $($exception.Message)" -ForegroundColor Red
        if ($fix) {
            Write-Host "Fix: $fix" -ForegroundColor Yellow
        }
        else {
            Write-Host "Location: $($_.InvocationInfo.PositionMessage)"
            Write-Host "Fix: rerun the demo; if it fails again, report this output. Runbook: $RunbookPath" -ForegroundColor Yellow
        }
    }

    if (@($script:Timings | Where-Object { $_.Section -ne 'Preflight' }).Count -gt 0) {
        Write-Finale -Sections $selected
    }
}
finally {
    Write-Host ''
    Write-Host '  Cleanup'
    if ($script:Started.Count -gt 0) {
        if ($KeepServers) {
            foreach ($entry in $script:Started) {
                $ids = @(Get-DescendantProcessId -ParentId $entry.ProcessId -NotBefore $entry.StartedAt) + @($entry.ProcessId)
                Write-Detail "Left the $($entry.Kind) server running on port $($entry.Port) (-KeepServers). Stop it with: Stop-Process -Id $($ids -join ',')"
            }
        }
        elseif (-not (Stop-StartedServer)) {
            $cleanupFailed = $true
        }
    }
    foreach ($kind in $script:Reused) {
        Write-Detail "The $kind server was already running before the demo; it was left running."
    }
    if ($script:Baseline) {
        if ($ResetDemoData) {
            $deleted = Invoke-DemoRowAction -Action 'delete'
            if ($deleted) {
                Write-Detail "Deleted the rows this run created: $($deleted.request_log) request_log, $($deleted.answer_feedback) answer_feedback, $($deleted.mock_tickets) mock_tickets (-ResetDemoData)."
            }
        }
        else {
            $added = Invoke-DemoRowAction -Action 'count'
            if ($added -and ([int]$added.request_log + [int]$added.answer_feedback + [int]$added.mock_tickets) -eq 0) {
                Write-Detail 'This run added no request_log, answer_feedback, or mock_tickets rows.'
            }
            elseif ($added) {
                Write-Detail "This run added $($added.request_log) request_log, $($added.answer_feedback) answer_feedback, and $($added.mock_tickets) mock_tickets rows; they are kept."
                Write-Detail "View them with: uv run python -m campus_rag.analytics --days 1   (tickets: GET $TicketUrl/tickets)"
                Write-Detail "Rerun with -ResetDemoData to delete the rows a run creates."
            }
        }
    }
    if ($script:HttpClient) {
        $script:HttpClient.Dispose()
    }
    foreach ($name in $savedEnv.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
    }
    if ($savedOutputEncoding) {
        try {
            [Console]::OutputEncoding = $savedOutputEncoding
        }
        catch {
            Write-Verbose 'Could not restore the console output encoding.'
        }
    }
    Pop-Location
}

if ($cleanupFailed -and $exitCode -eq 0) {
    $exitCode = 30
}
if ($exitCode -eq 0) {
    Write-Host ("Demo complete in {0:N0}s." -f $ScriptTimer.Elapsed.TotalSeconds) -ForegroundColor Green
}
exit $exitCode
