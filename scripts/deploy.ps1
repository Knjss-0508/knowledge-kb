param(
    [ValidateSet("auto", "local", "cloud")]
    [string]$DatabaseMode = $(if ($env:DEPLOY_DATABASE_MODE) { $env:DEPLOY_DATABASE_MODE } else { "auto" }),
    [Alias("EmbeddingMode")]
    [ValidateSet("auto", "gpu", "cpu")]
    [string]$Runtime = $(
        if ($env:DEPLOY_RUNTIME) { $env:DEPLOY_RUNTIME }
        elseif ($env:EMBEDDING_MODE) { $env:EMBEDDING_MODE }
        else { "auto" }
    ),
    [int]$TimeoutSeconds = $(if ($env:DEPLOY_TIMEOUT_SECONDS) { [int]$env:DEPLOY_TIMEOUT_SECONDS } else { 900 })
)

$ErrorActionPreference = "Stop"
$ProjectName = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { "knowledge-kb" }
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Install Docker Engine/Desktop before deploying."
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required."
}

function Invoke-DockerWithTimeout(
    [string[]]$Arguments,
    [int]$CommandTimeoutSeconds
) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = (Get-Command docker).Source
    $startInfo.UseShellExecute = $false
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start Docker."
    }
    if (-not $process.WaitForExit($CommandTimeoutSeconds * 1000)) {
        try {
            $process.Kill($true)
        } catch {
            Write-Warning "Timed-out Docker process could not be terminated cleanly."
        }
        return 124
    }
    return $process.ExitCode
}

function Stop-InitializationContainers([string[]]$Arguments) {
    try {
        & docker @Arguments stop -t 10 migrate *> $null
    } catch {
        Write-Warning "Timed-out database initialization container could not be stopped cleanly."
    }
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Review secrets before production use."
}

function Get-DotEnvValue([string]$Name) {
    $line = Get-Content ".env" |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -Last 1
    if ($line) {
        return $line.Substring($Name.Length + 1)
    }
    return $null
}

function Get-ConfiguredValue([string]$Name) {
    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if ($processValue) {
        return $processValue
    }
    return Get-DotEnvValue $Name
}

if (-not $PSBoundParameters.ContainsKey("DatabaseMode") -and -not $env:DEPLOY_DATABASE_MODE) {
    $configuredDatabaseMode = Get-DotEnvValue "DEPLOY_DATABASE_MODE"
    if ($configuredDatabaseMode) { $DatabaseMode = $configuredDatabaseMode }
}
if (-not $PSBoundParameters.ContainsKey("Runtime") -and -not $env:DEPLOY_RUNTIME -and -not $env:EMBEDDING_MODE) {
    $configuredRuntime = Get-DotEnvValue "DEPLOY_RUNTIME"
    if (-not $configuredRuntime) { $configuredRuntime = Get-DotEnvValue "EMBEDDING_MODE" }
    if ($configuredRuntime) { $Runtime = $configuredRuntime }
}
if (-not $PSBoundParameters.ContainsKey("TimeoutSeconds") -and -not $env:DEPLOY_TIMEOUT_SECONDS) {
    $configuredTimeout = Get-DotEnvValue "DEPLOY_TIMEOUT_SECONDS"
    if ($configuredTimeout) { $TimeoutSeconds = [int]$configuredTimeout }
}

if (@("auto", "local", "cloud") -notcontains $DatabaseMode) {
    throw "DEPLOY_DATABASE_MODE must be auto, local, or cloud."
}
if (@("auto", "gpu", "cpu") -notcontains $Runtime) {
    throw "DEPLOY_RUNTIME or EMBEDDING_MODE must be auto, gpu, or cpu."
}
if ($TimeoutSeconds -le 0) {
    throw "DEPLOY_TIMEOUT_SECONDS must be a positive integer."
}

$DatabaseUrl = Get-ConfiguredValue "DATABASE_URL"
if ($DatabaseMode -eq "auto") {
    $DatabaseMode = if ($DatabaseUrl) { "cloud" } else { "local" }
}

if ($DatabaseMode -eq "cloud") {
    if (-not $DatabaseUrl -or $DatabaseUrl -match "replace-with|db\.example\.com") {
        throw "Set a real DATABASE_URL when DEPLOY_DATABASE_MODE=cloud."
    }
    if ($DatabaseUrl -notmatch "^(postgres|postgresql|postgresql\+psycopg2)://") {
        throw "DATABASE_URL must use a PostgreSQL connection scheme."
    }
    if ((Get-ConfiguredValue "MEDIA_STORAGE_BACKEND") -ne "s3") {
        throw "MEDIA_STORAGE_BACKEND=s3 is required in cloud database mode."
    }
    $S3Bucket = Get-ConfiguredValue "S3_BUCKET"
    if (-not $S3Bucket -or $S3Bucket -match "replace-with") {
        throw "Set a real S3_BUCKET in cloud database mode."
    }
    $S3AccessKey = Get-ConfiguredValue "S3_ACCESS_KEY_ID"
    $S3SecretKey = Get-ConfiguredValue "S3_SECRET_ACCESS_KEY"
    if ([bool]$S3AccessKey -ne [bool]$S3SecretKey) {
        throw "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together."
    }
    if ((Get-ConfiguredValue "S3_SESSION_TOKEN") -and -not $S3AccessKey) {
        throw "S3_SESSION_TOKEN requires S3 access key credentials."
    }
    $S3Endpoint = Get-ConfiguredValue "S3_ENDPOINT_URL"
    if ($S3Endpoint -and $S3Endpoint -notmatch "^https?://") {
        throw "S3_ENDPOINT_URL must start with http:// or https://."
    }
    $AdminUsername = Get-ConfiguredValue "INITIAL_ADMIN_USERNAME"
    $AdminPassword = Get-ConfiguredValue "INITIAL_ADMIN_PASSWORD"
    if ([bool]$AdminUsername -ne [bool]$AdminPassword) {
        throw "INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must be set together."
    }
    if ($AdminPassword -and ($AdminPassword.Length -lt 12 -or $AdminPassword -match "replace-with")) {
        throw "INITIAL_ADMIN_PASSWORD must contain at least 12 characters."
    }
    if ((Get-ConfiguredValue "INITIAL_ADMIN_FORCE_RESET") -eq "true" -and -not $AdminPassword) {
        throw "INITIAL_ADMIN_FORCE_RESET=true requires administrator credentials."
    }
    $IntegrationApiKey = Get-ConfiguredValue "INTEGRATION_API_KEY"
    if (-not $IntegrationApiKey -or $IntegrationApiKey.Length -lt 24 -or $IntegrationApiKey -match "replace-with") {
        throw "Set INTEGRATION_API_KEY to a non-placeholder secret of at least 24 characters."
    }
    if ((Get-ConfiguredValue "ALLOW_INSECURE_DEFAULT_ADMIN") -ne "false") {
        throw "Set ALLOW_INSECURE_DEFAULT_ADMIN=false in cloud database mode."
    }
    $EmbeddingDimensions = Get-ConfiguredValue "EMBEDDING_DIMENSIONS"
    if ($EmbeddingDimensions -and $EmbeddingDimensions -ne "1024") {
        throw "Cloud deployment must preserve EMBEDDING_DIMENSIONS=1024."
    }
}

$TeiGpuImage = if ($env:TEI_GPU_IMAGE) {
    $env:TEI_GPU_IMAGE
} else {
    $configuredImage = Get-DotEnvValue "TEI_GPU_IMAGE"
    if ($configuredImage) { $configuredImage } else { "ghcr.io/huggingface/text-embeddings-inference:cuda-1.8.3" }
}

function Test-GpuRuntime {
    & docker run --rm --gpus all --entrypoint /bin/sh $TeiGpuImage -c "exit 0" *> $null
    return $LASTEXITCODE -eq 0
}

$SelectedRuntime = $Runtime
if ($Runtime -eq "auto") {
    $SelectedRuntime = if (Test-GpuRuntime) { "gpu" } else { "cpu" }
} elseif ($Runtime -eq "gpu" -and -not (Test-GpuRuntime)) {
    throw "The configured GPU profile is incompatible with this Docker/NVIDIA runtime. Use -Runtime cpu or install a compatible driver."
}

$OverrideFile = if ($SelectedRuntime -eq "gpu") {
    "docker-compose.embedding-gpu.yml"
} else {
    "docker-compose.embedding-cpu.yml"
}
if ($SelectedRuntime -eq "gpu") {
    $env:TEI_GPU_IMAGE = $TeiGpuImage
}

$ComposeArgs = @("compose", "-p", $ProjectName, "-f", "docker-compose.yml")
if ($DatabaseMode -eq "local") {
    $ComposeArgs += @("-f", "docker-compose.local.yml")
}
$ComposeArgs += @("-f", $OverrideFile)

Write-Host "Database mode: $DatabaseMode"
Write-Host "Selected runtime: $SelectedRuntime"
& docker @ComposeArgs config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose configuration validation failed."
}
if ($DatabaseMode -eq "cloud") {
    $configuredServices = @(& docker @ComposeArgs config --services)
    if ($configuredServices -contains "postgres") {
        throw "Cloud deployment configuration unexpectedly contains a local PostgreSQL service."
    }
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$RemainingSeconds = [Math]::Max(
    1,
    [int][Math]::Ceiling(($Deadline - (Get-Date)).TotalSeconds)
)
$UpExitCode = Invoke-DockerWithTimeout (
    $ComposeArgs + @("up", "-d", "--build", "--remove-orphans")
) $RemainingSeconds
if ($UpExitCode -ne 0) {
    & docker @ComposeArgs logs --tail 150 migrate
    if ($UpExitCode -eq 124) {
        Stop-InitializationContainers $ComposeArgs
        throw "Docker Compose startup timed out after $TimeoutSeconds seconds."
    }
    throw "Docker Compose failed to start the selected runtime."
}

$MigrateContainer = (& docker @ComposeArgs ps -a -q migrate).Trim()
if ($MigrateContainer) {
    $MigrateExitCode = (& docker inspect --format "{{.State.ExitCode}}" $MigrateContainer).Trim()
    if ($MigrateExitCode -ne "0") {
        & docker @ComposeArgs logs --tail 150 migrate
        throw "Database initialization failed."
    }
}

$BackendReady = $false
while ((Get-Date) -lt $Deadline) {
    & docker @ComposeArgs exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).read()" *> $null
    if ($LASTEXITCODE -eq 0) {
        $BackendReady = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $BackendReady) {
    & docker @ComposeArgs ps
    & docker @ComposeArgs logs --tail 100 migrate embedding-qwen backend
    Stop-InitializationContainers $ComposeArgs
    throw "Database, backend, or embedding readiness timed out after $TimeoutSeconds seconds."
}

$RemainingSeconds = [Math]::Max(
    1,
    [int][Math]::Ceiling(($Deadline - (Get-Date)).TotalSeconds)
)
$EmbeddingExitCode = Invoke-DockerWithTimeout (
    $ComposeArgs + @("exec", "-T", "backend", "python", "-m", "app.scripts.smoke_embedding")
) $RemainingSeconds
if ($EmbeddingExitCode -ne 0) {
    & docker @ComposeArgs logs --tail 100 embedding-qwen backend
    if ($EmbeddingExitCode -eq 124) {
        throw "Embedding smoke test timed out."
    }
    throw "Embedding smoke test failed."
}

# Run the destructive media put/get/delete probe exactly once after readiness.
$RemainingSeconds = [Math]::Max(
    1,
    [int][Math]::Ceiling(($Deadline - (Get-Date)).TotalSeconds)
)
$MediaExitCode = Invoke-DockerWithTimeout (
    $ComposeArgs + @("exec", "-T", "backend", "python", "-m", "app.scripts.smoke_media_storage")
) $RemainingSeconds
if ($MediaExitCode -ne 0) {
    & docker @ComposeArgs logs --tail 100 backend
    if ($MediaExitCode -eq 124) {
        throw "Media storage smoke test timed out."
    }
    throw "Media storage smoke test failed."
}

& docker @ComposeArgs ps
Write-Host "Deployment completed: database=$DatabaseMode, runtime=$SelectedRuntime"
