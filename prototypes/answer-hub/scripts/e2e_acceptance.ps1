param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$IntegrationKey = $env:INTEGRATION_API_KEY,
    [string]$Username = "",
    [string]$Password = "",
    [switch]$RunMutationTests,
    [switch]$PublishTestKnowledge
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$result = [ordered]@{
    started_at = (Get-Date).ToString("o")
    base_url = $BaseUrl
    checks = @()
}

function Add-Check {
    param([string]$Name, [string]$Status, [object]$Detail)
    $script:result.checks += [ordered]@{
        name = $Name
        status = $Status
        detail = $Detail
    }
}

try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10
    Add-Check "health" "passed" $health
    $ready = Invoke-RestMethod -Uri "$BaseUrl/ready" -TimeoutSec 15
    Add-Check "ready" "passed" $ready

    if (-not $IntegrationKey) {
        throw "INTEGRATION_API_KEY is required for taxonomy and batch acceptance."
    }
    $integrationHeaders = @{ "X-Integration-Key" = $IntegrationKey }
    $taxonomy = Invoke-RestMethod -Uri "$BaseUrl/api/v1/integration/taxonomy" `
        -Headers $integrationHeaders -TimeoutSec 15
    Add-Check "taxonomy" "passed" @{
        version = $taxonomy.version
        categories = @($taxonomy.categories).Count
    }

    if ($RunMutationTests) {
        $examplePath = Join-Path $workspace "examples\second_part_batch.example.json"
        $batch = Get-Content -LiteralPath $examplePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $suffix = Get-Date -Format "yyyyMMddHHmmss"
        foreach ($item in $batch.items) {
            $item.event_id = "$($item.event_id)-$suffix"
            $item.idempotency_key = "$($item.idempotency_key)-$suffix"
        }
        $body = $batch | ConvertTo-Json -Depth 20
        $first = Invoke-RestMethod `
            -Uri "$BaseUrl/api/v1/integration/second-part/records:batch" `
            -Method Post -Headers $integrationHeaders `
            -ContentType "application/json" -Body $body -TimeoutSec 7200
        $second = Invoke-RestMethod `
            -Uri "$BaseUrl/api/v1/integration/second-part/records:batch" `
            -Method Post -Headers $integrationHeaders `
            -ContentType "application/json" -Body $body -TimeoutSec 120
        if ([int]$second.reused -lt @($batch.items).Count) {
            throw "Idempotency retry did not reuse every submitted item."
        }
        Add-Check "second_part_idempotency" "passed" @{
            accepted = $first.accepted
            reused = $second.reused
            topic_rows = $first.topic_rows
        }
    }

    if ($PublishTestKnowledge) {
        if (-not $Username -or -not $Password) {
            throw "Username and Password are required for review/publish acceptance."
        }
        $login = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/login" -Method Post `
            -ContentType "application/json" `
            -Body (@{ username = $Username; password = $Password } | ConvertTo-Json)
        $authHeaders = @{ Authorization = "Bearer $($login.token)" }
        $candidates = Invoke-RestMethod -Uri "$BaseUrl/api/v1/topic-candidates?workflow_status=pending_manual_review" `
            -Headers $authHeaders
        if (-not @($candidates).Count) {
            throw "No pending topic candidate is available for publish acceptance."
        }
        $candidate = @($candidates)[0]
        $category = @($taxonomy.categories | Where-Object { $_.level -ge 2 })[0]
        if (-not $category) { $category = @($taxonomy.categories)[0] }
        $draft = if ($candidate.final_draft.Count) { $candidate.final_draft } else { $candidate.transcription_draft }
        $passDecision = [regex]::Unescape('\u901a\u8fc7')
        $reviewBody = @{
            final_draft = $draft
            reviewer_decision = $passDecision
            reviewer_error_type = ""
            reviewer_error_reason = ""
            reviewer_notes = "End-to-end acceptance sample"
            include_in_training = $false
            redaction_confirmed = $true
            target_category_id = $category.id
            target_layer = "L2"
        } | ConvertTo-Json -Depth 20
        Invoke-RestMethod -Uri "$BaseUrl/api/v1/topic-candidates/$($candidate.id)/review" `
            -Method Post -Headers $authHeaders -ContentType "application/json" -Body $reviewBody | Out-Null
        $submitted = Invoke-RestMethod -Uri "$BaseUrl/api/v1/topic-candidates/$($candidate.id)/submit-to-cz-review" `
            -Method Post -Headers $authHeaders
        $knowledgeId = $submitted.knowledge_id
        Invoke-RestMethod -Uri "$BaseUrl/api/v1/knowledge/$knowledgeId/approve" `
            -Method Post -Headers $authHeaders | Out-Null
        $search = Invoke-RestMethod -Uri "$BaseUrl/api/v1/knowledge/search" `
            -Method Post -Headers $authHeaders -ContentType "application/json" `
            -Body (@{ query = $candidate.title; top_k = 10 } | ConvertTo-Json)
        Invoke-RestMethod -Uri "$BaseUrl/api/v1/knowledge/feedback" `
            -Method Post -Headers $authHeaders -ContentType "application/json" `
            -Body (@{ knowledge_id = $knowledgeId; action = "useful"; session_id = "e2e-$suffix" } | ConvertTo-Json) | Out-Null
        Add-Check "review_publish_search_feedback" "passed" @{
            knowledge_id = $knowledgeId
            search_total = $search.total
        }
    }

    $result.status = "passed"
}
catch {
    Add-Check "failure" "failed" $_.Exception.Message
    $result.status = "failed"
    $result.error = $_.Exception.Message
}

$result.finished_at = (Get-Date).ToString("o")
$result | ConvertTo-Json -Depth 20
if ($result.status -ne "passed") { exit 1 }
