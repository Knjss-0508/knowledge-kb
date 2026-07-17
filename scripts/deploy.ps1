param(
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

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Review secrets before production use."
}

function Get-DotEnvValue([string]$Name) {
    $line = Get-Content ".env" | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if ($line) {
        return $line.Substring($Name.Length + 1)
    }
    return $null
}

if (-not $PSBoundParameters.ContainsKey("Runtime") -and -not $env:DEPLOY_RUNTIME -and -not $env:EMBEDDING_MODE) {
    $configuredRuntime = Get-DotEnvValue "DEPLOY_RUNTIME"
    if (-not $configuredRuntime) { $configuredRuntime = Get-DotEnvValue "EMBEDDING_MODE" }
    if ($configuredRuntime) { $Runtime = $configuredRuntime }
}
if (@("auto", "gpu", "cpu") -notcontains $Runtime) {
    throw "DEPLOY_RUNTIME or EMBEDDING_MODE must be auto, gpu, or cpu."
}
if (-not $PSBoundParameters.ContainsKey("TimeoutSeconds") -and -not $env:DEPLOY_TIMEOUT_SECONDS) {
    $configuredTimeout = Get-DotEnvValue "DEPLOY_TIMEOUT_SECONDS"
    if ($configuredTimeout) { $TimeoutSeconds = [int]$configuredTimeout }
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

$OverrideFile = if ($SelectedRuntime -eq "gpu") { "docker-compose.embedding-gpu.yml" } else { "docker-compose.embedding-cpu.yml" }
if ($SelectedRuntime -eq "gpu") {
    $env:TEI_GPU_IMAGE = $TeiGpuImage
}

$ComposeArgs = @("compose", "-p", $ProjectName, "-f", "docker-compose.yml", "-f", $OverrideFile)
Write-Host "Selected runtime: $SelectedRuntime"
& docker @ComposeArgs up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to start the selected runtime."
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $Deadline) {
    & docker @ComposeArgs exec -T backend python -m app.scripts.smoke_embedding *> $null
    if ($LASTEXITCODE -eq 0) {
        & docker @ComposeArgs ps
        Write-Host "Deployment completed with runtime: $SelectedRuntime"
        exit 0
    }
    Start-Sleep -Seconds 2
}

& docker @ComposeArgs ps
& docker @ComposeArgs logs --tail 100 embedding-qwen backend
throw "Embedding smoke test timed out after $TimeoutSeconds seconds."
