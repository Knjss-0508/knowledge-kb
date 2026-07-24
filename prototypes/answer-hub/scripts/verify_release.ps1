param(
    [string]$PackagePath = "",
    [switch]$BuildPackage,
    [string]$Version = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [switch]$SkipCompose,
    [switch]$RunE2E,
    [string]$E2EBaseUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$rootPython = Join-Path $workspace ".venv\Scripts\python.exe"
$czPython = Join-Path $workspace ".cz_test_venv\Scripts\python.exe"
$czRoot = Join-Path $workspace "cz-knowledge-kb\knowledge-kb-master"
$czBackend = Join-Path $czRoot "backend"
$frontendPath = Join-Path $czRoot "frontend\index.html"
$composePath = Join-Path $czRoot "docker-compose.yml"
$steps = [System.Collections.Generic.List[object]]::new()
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$verificationTempParent = Join-Path $workspace "outputs"
$verificationTempRoot = Join-Path $verificationTempParent (
    "release-verification-" + [guid]::NewGuid().ToString("N")
)

if ($BuildPackage -and $PackagePath) {
    throw "-BuildPackage and -PackagePath cannot be used together."
}

function Invoke-ReleaseStep {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    $startedAt = Get-Date
    Write-Host "==> $Name" -ForegroundColor Cyan
    try {
        & $Action
        $steps.Add([ordered]@{
            name = $Name
            status = "passed"
            seconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
        })
    }
    catch {
        $steps.Add([ordered]@{
            name = $Name
            status = "failed"
            seconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
            error = $_.Exception.Message
        })
        throw
    }
}

function Assert-CommandSucceeded {
    param([string]$Description)
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Remove-VerificationTemp {
    $parent = [System.IO.Path]::GetFullPath($verificationTempParent).TrimEnd("\") + "\"
    $target = [System.IO.Path]::GetFullPath($verificationTempRoot)
    if (
        (Test-Path -LiteralPath $target) -and
        $target.StartsWith($parent, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $target).StartsWith("release-verification-")
    ) {
        try {
            Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Verification temp cleanup reported an error: $($_.Exception.Message)"
        }
    }
}

function Get-StableFileHashValue {
    param(
        [string]$Path,
        [int]$MaxAttempts = 4
    )
    $lastError = $null
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        }
        catch {
            $lastError = $_
            if ($attempt -lt $MaxAttempts) {
                Start-Sleep -Milliseconds (150 * $attempt)
            }
        }
    }
    throw $lastError
}

function Test-DeliveryPackage {
    param([string]$InputPath)

    $resolvedInput = [System.IO.Path]::GetFullPath($InputPath)
    if (-not (Test-Path -LiteralPath $resolvedInput)) {
        throw "Delivery package does not exist: $resolvedInput"
    }

    # Keep package extraction shallow enough for Windows MAX_PATH while still
    # constraining cleanup to the workspace outputs directory.
    $tempBase = [System.IO.Path]::GetFullPath($verificationTempParent)
    $tempRoot = Join-Path $tempBase ("kbpa-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    try {
        $item = Get-Item -LiteralPath $resolvedInput
        if ($item.PSIsContainer) {
            $packageRoot = $item.FullName
        }
        elseif ($item.Extension.ToLowerInvariant() -eq ".zip") {
            Expand-Archive -LiteralPath $item.FullName -DestinationPath $tempRoot
            $roots = @(Get-ChildItem -LiteralPath $tempRoot -Directory)
            $packageRoot = if ($roots.Count -eq 1) { $roots[0].FullName } else { $tempRoot }
        }
        else {
            throw "PackagePath must be a delivery directory or a .zip file."
        }

        $forbiddenDirectories = @(
            ".git", ".venv", ".cz_test_venv", "node_modules",
            "__pycache__", ".pytest_cache", "outputs", "data", "uploads"
        )
        $forbiddenNames = @(".env")
        $forbiddenExtensions = @(
            ".db", ".sqlite", ".sqlite3", ".xlsx", ".xls", ".csv",
            ".pkl", ".npy", ".npz", ".pem", ".key", ".log"
        )
        $violations = [System.Collections.Generic.List[string]]::new()
        foreach ($file in Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Force) {
            $relative = $file.FullName.Substring($packageRoot.Length).TrimStart("\")
            $parts = $relative.Split([System.IO.Path]::DirectorySeparatorChar)
            if ($forbiddenNames -contains $file.Name) {
                $violations.Add($relative)
                continue
            }
            if ($forbiddenExtensions -contains $file.Extension.ToLowerInvariant()) {
                $violations.Add($relative)
                continue
            }
            if (@($parts | Where-Object { $forbiddenDirectories -contains $_ }).Count -gt 0) {
                $violations.Add($relative)
            }
        }
        if ($violations.Count -gt 0) {
            throw "Delivery package contains forbidden files: $($violations -join ', ')"
        }

        $manifestPath = Join-Path $packageRoot "manifest.json"
        if (-not (Test-Path -LiteralPath $manifestPath)) {
            throw "Delivery package is missing manifest.json."
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $manifest.excludes_secrets_and_business_data) {
            throw "manifest.json does not confirm secret and business-data exclusion."
        }

        $checksumsPath = Join-Path $packageRoot "checksums.sha256"
        if (-not (Test-Path -LiteralPath $checksumsPath)) {
            throw "Delivery package is missing checksums.sha256."
        }
        foreach ($line in Get-Content -LiteralPath $checksumsPath -Encoding UTF8) {
            if (-not $line.Trim()) {
                continue
            }
            $checksumMatch = [regex]::Match($line, "^([0-9a-fA-F]{64})  (.+)$")
            if (-not $checksumMatch.Success) {
                throw "Invalid checksums.sha256 line: $line"
            }
            $expected = $checksumMatch.Groups[1].Value.ToLowerInvariant()
            $relative = $checksumMatch.Groups[2].Value
            $target = [System.IO.Path]::GetFullPath((Join-Path $packageRoot $relative))
            if (-not $target.StartsWith(
                [System.IO.Path]::GetFullPath($packageRoot),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Checksum path escapes package root: $relative"
            }
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
                throw "Checksum target is missing: $relative"
            }
            $actual = Get-StableFileHashValue -Path $target
            if ($actual -ne $expected) {
                throw "Checksum mismatch: $relative"
            }
        }
        Write-Host "    Package checksums verified." -ForegroundColor DarkGray

        $previousPythonPath = $env:PYTHONPATH
        $previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
        try {
            $env:PYTHONPATH = Join-Path $packageRoot "src"
            $env:PYTHONDONTWRITEBYTECODE = "1"
            Push-Location $packageRoot
            try {
                & $rootPython -m pytest -q -p no:cacheprovider `
                    --basetemp (Join-Path $tempRoot "pytest")
                Assert-CommandSucceeded "Packaged root test suite"
                Write-Host "    Packaged root tests passed." -ForegroundColor DarkGray
            }
            finally {
                Pop-Location
            }

            $packagedCzBackend = Join-Path $packageRoot "cz-knowledge-kb\knowledge-kb-master\backend"
            Push-Location $packagedCzBackend
            try {
                & $czPython -m unittest discover -s tests -v
                Assert-CommandSucceeded "Packaged CZ backend test suite"
                Write-Host "    Packaged CZ backend tests passed." -ForegroundColor DarkGray
            }
            finally {
                Pop-Location
            }
        }
        finally {
            $env:PYTHONPATH = $previousPythonPath
            $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
        }
    }
    finally {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        Write-Host "    Cleaning temporary verification directory." -ForegroundColor DarkGray
        if (
            (Test-Path -LiteralPath $resolvedTempRoot) -and
            $resolvedTempRoot.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedTempRoot).StartsWith("kbpa-")
        ) {
            try {
                Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force -ErrorAction Stop
            }
            catch {
                Write-Warning "Temporary verification cleanup reported an error: $($_.Exception.Message)"
            }
            if (Test-Path -LiteralPath $resolvedTempRoot) {
                Write-Warning "Temporary verification directory could not be fully removed: $resolvedTempRoot"
            }
        }
    }
}

try {
    New-Item -ItemType Directory -Path $verificationTempRoot -Force | Out-Null
    $env:TEMP = $verificationTempRoot
    $env:TMP = $verificationTempRoot

    Invoke-ReleaseStep "Root test suite" {
        if (-not (Test-Path -LiteralPath $rootPython)) {
            throw "Root Python environment does not exist: $rootPython"
        }
        Push-Location $workspace
        try {
            & $rootPython -m pytest -q -p no:cacheprovider `
                --basetemp (Join-Path $verificationTempRoot "root-pytest")
            Assert-CommandSucceeded "Root test suite"
        }
        finally {
            Pop-Location
        }
    }

    Invoke-ReleaseStep "CZ backend test suite" {
        if (-not (Test-Path -LiteralPath $czPython)) {
            throw "CZ Python environment does not exist: $czPython"
        }
        Push-Location $czBackend
        try {
            & $czPython -m unittest discover -s tests -v
            Assert-CommandSucceeded "CZ backend test suite"
        }
        finally {
            Pop-Location
        }
    }

    Invoke-ReleaseStep "Python compileall" {
        & $rootPython -m compileall -q `
            (Join-Path $workspace "src") `
            (Join-Path $workspace "tests") `
            (Join-Path $workspace "streamlit_app.py") `
            (Join-Path $czBackend "app") `
            (Join-Path $czBackend "tests")
        Assert-CommandSucceeded "Python compileall"
    }

    Invoke-ReleaseStep "Sensitive artifact scan" {
        & $rootPython (Join-Path $workspace "scripts\scan_sensitive_files.py") `
            --root $workspace `
            --ignore-local-env `
            --ignore-local-runtime
        Assert-CommandSucceeded "Sensitive artifact scan"
    }

    Invoke-ReleaseStep "CZ frontend inline JavaScript syntax" {
        $node = Get-Command node -ErrorAction Stop
        $html = Get-Content -LiteralPath $frontendPath -Raw -Encoding UTF8
        $matches = [regex]::Matches(
            $html,
            "<script(?<attrs>[^>]*)>(?<code>.*?)</script>",
            [System.Text.RegularExpressions.RegexOptions]::Singleline
        )
        $inlineScripts = @(
            $matches |
                Where-Object { $_.Groups["attrs"].Value -notmatch "\bsrc\s*=" } |
                ForEach-Object { $_.Groups["code"].Value }
        )
        if ($inlineScripts.Count -eq 0) {
            throw "No inline JavaScript was found in the CZ frontend."
        }
        $tempJs = Join-Path ([System.IO.Path]::GetTempPath()) ("cz-frontend-" + [guid]::NewGuid().ToString("N") + ".js")
        try {
            $inlineScripts -join "`n" | Set-Content -LiteralPath $tempJs -Encoding UTF8
            & $node.Source --check $tempJs
            Assert-CommandSucceeded "CZ frontend JavaScript syntax"
        }
        finally {
            Remove-Item -LiteralPath $tempJs -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not $SkipCompose) {
        Invoke-ReleaseStep "Docker Compose configuration" {
            $docker = Get-Command docker -ErrorAction Stop
            & $docker.Source compose -f $composePath config --quiet
            Assert-CommandSucceeded "Docker Compose configuration"
        }
    }

    if ($RunE2E) {
        Invoke-ReleaseStep "Running-service end-to-end acceptance" {
            & (Join-Path $PSScriptRoot "e2e_acceptance.ps1") `
                -BaseUrl $E2EBaseUrl `
                -IntegrationKey $env:INTEGRATION_API_KEY `
                -RunMutationTests
            Assert-CommandSucceeded "Running-service end-to-end acceptance"
        }
    }

    if ($BuildPackage) {
        Invoke-ReleaseStep "Build delivery package" {
            & (Join-Path $PSScriptRoot "build_delivery_package.ps1") -Version $Version | Out-Host
            $expectedPackage = Join-Path $workspace "handoff\answer-hub-delivery-$Version.zip"
            if (-not (Test-Path -LiteralPath $expectedPackage -PathType Leaf)) {
                throw "Delivery package build did not create the expected zip: $expectedPackage"
            }
            $script:PackagePath = $expectedPackage
        }
    }

    if ($PackagePath) {
        Invoke-ReleaseStep "Delivery package scan and packaged tests" {
            Test-DeliveryPackage -InputPath $PackagePath
        }
    }

    [ordered]@{
        status = "passed"
        verified_at = (Get-Date).ToString("o")
        package_path = $PackagePath
        steps = $steps
    } | ConvertTo-Json -Depth 6
}
catch {
    [ordered]@{
        status = "failed"
        verified_at = (Get-Date).ToString("o")
        package_path = $PackagePath
        error = $_.Exception.Message
        steps = $steps
    } | ConvertTo-Json -Depth 6
    exit 1
}
finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    Remove-VerificationTemp
}
