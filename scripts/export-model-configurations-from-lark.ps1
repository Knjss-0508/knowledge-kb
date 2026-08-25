[CmdletBinding()]
param(
    [Parameter()]
    [string]$SpreadsheetToken = "TLxlsXMKJhPn1htD31lcdl2enKd",

    [Parameter()]
    [string]$SheetId = "w3Caff",

    [Parameter()]
    [string]$Range = "",

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$env:LARKSUITE_CLI_NO_UPDATE_NOTIFIER = "1"
$env:LARKSUITE_CLI_NO_SKILLS_NOTIFIER = "1"

if (-not (Get-Command lark-cli -ErrorAction SilentlyContinue)) {
    throw "未找到 lark-cli，请先安装并完成用户授权。"
}

if (-not $Range.Trim()) {
    $workbookInfoRaw = (
        & lark-cli sheets +workbook-info `
            --as user `
            --spreadsheet-token $SpreadsheetToken `
            --json |
            Out-String
    )
    if ($LASTEXITCODE -ne 0) {
        throw "lark-cli 读取工作簿信息失败，退出码：$LASTEXITCODE"
    }
    $workbookInfo = $workbookInfoRaw | ConvertFrom-Json -Depth 100
    $targetSheet = @($workbookInfo.data.sheets) |
        Where-Object { $_.sheet_id -eq $SheetId } |
        Select-Object -First 1
    if (-not $targetSheet -or [int]$targetSheet.row_count -lt 1) {
        throw "未找到目标工作表或工作表行数无效：$SheetId"
    }
    $Range = "A1:Q$([int]$targetSheet.row_count)"
}

$rawOutput = (
    & lark-cli sheets +cells-get `
        --as user `
        --spreadsheet-token $SpreadsheetToken `
        --sheet-id $SheetId `
        --range $Range `
        --include value `
        --max-chars 20000000 `
        --json |
        Out-String
)
if ($LASTEXITCODE -ne 0) {
    throw "lark-cli 读取飞书表格失败，退出码：$LASTEXITCODE"
}

$response = $rawOutput | ConvertFrom-Json -Depth 100
if (-not $response.ok -or $response.data.has_more) {
    throw "飞书表格读取不完整，禁止生成同步文件。"
}
$sheetRange = @($response.data.ranges)[0]
if (-not $sheetRange -or $sheetRange.truncated) {
    throw "飞书表格结果为空或被截断，禁止生成同步文件。"
}

$columns = @($sheetRange.col_indices)
$rows = @($sheetRange.cells)
$rowIndices = @($sheetRange.row_indices)
if ($rows.Count -lt 2) {
    throw "飞书表格没有可同步的数据行。"
}

$headers = @{}
for ($columnIndex = 0; $columnIndex -lt $columns.Count; $columnIndex += 1) {
    $headerValue = [string]($rows[0][$columnIndex].value)
    if ($headerValue.Trim()) {
        $headerName = $headerValue.Trim()
        if ($headers.ContainsKey($headerName)) {
            throw "飞书表格存在重复表头：$headerName"
        }
        $headers[$headerName] = $columnIndex
    }
}

$requiredHeaders = @(
    "标题",
    "品牌ID",
    "品牌",
    "型号ID",
    "型号",
    "综合内容"
)
$ignoredSourceFieldHeaders = @(
    "是否有卡槽",
    "Home键",
    "指纹识别",
    "3D面容",
    "内置手写笔",
    "闪光灯",
    "蜂窝网络",
    "光线传感器"
)
foreach ($requiredHeader in $requiredHeaders) {
    if (-not $headers.ContainsKey($requiredHeader)) {
        throw "飞书表格缺少必填列：$requiredHeader"
    }
}

function Get-CellText {
    param(
        [object[]]$Row,
        [string]$Header
    )
    if (-not $headers.ContainsKey($Header)) {
        return ""
    }
    $index = [int]$headers[$Header]
    if ($index -ge $Row.Count -or $null -eq $Row[$index].value) {
        return ""
    }
    return ([string]$Row[$index].value).Trim()
}

$records = [System.Collections.Generic.List[object]]::new()
$seenModelKeys = [System.Collections.Generic.HashSet[string]]::new()
for ($rowIndex = 1; $rowIndex -lt $rows.Count; $rowIndex += 1) {
    $row = @($rows[$rowIndex])
    $sourceRowNumber = if (
        $rowIndex -lt $rowIndices.Count -and
        $null -ne $rowIndices[$rowIndex]
    ) {
        [string]$rowIndices[$rowIndex]
    } else {
        [string]($rowIndex + 1)
    }
    $requiredValues = [ordered]@{
        "标题" = Get-CellText -Row $row -Header "标题"
        "品牌ID" = Get-CellText -Row $row -Header "品牌ID"
        "品牌" = Get-CellText -Row $row -Header "品牌"
        "型号ID" = Get-CellText -Row $row -Header "型号ID"
        "型号" = Get-CellText -Row $row -Header "型号"
        "综合内容" = Get-CellText -Row $row -Header "综合内容"
    }
    $populatedRequiredValues = @(
        $requiredValues.GetEnumerator() |
            Where-Object { [string]$_.Value }
    )
    if (-not $populatedRequiredValues.Count) {
        continue
    }
    $missingHeaders = @(
        $requiredValues.GetEnumerator() |
            Where-Object { -not [string]$_.Value } |
            ForEach-Object { $_.Key }
    )
    if ($missingHeaders.Count) {
        throw (
            "飞书表格第 $sourceRowNumber 行缺少必填字段：" +
            ($missingHeaders -join "、")
        )
    }

    $sourceRecordId = Get-CellText -Row $row -Header "知识ID"
    $brandId = [string]$requiredValues["品牌ID"]
    $modelId = [string]$requiredValues["型号ID"]
    $modelKey = "119|$brandId|$modelId"
    if (-not $seenModelKeys.Add($modelKey)) {
        throw "飞书表格品类/品牌/型号ID组合重复：119/$brandId/$modelId"
    }

    $sourceFields = [ordered]@{}
    foreach ($header in $headers.Keys) {
        if ($ignoredSourceFieldHeaders -contains $header) {
            continue
        }
        $value = Get-CellText -Row $row -Header $header
        if ($value) {
            $sourceFields[$header] = $value
        }
    }
    $sourceFields["来源工作表"] = "个性化配置信息"
    $sourceFields["来源行号"] = $sourceRowNumber
    $sourceFields["品类ID"] = "119"
    $sourceFields["品类"] = "平板电脑"

    $records.Add(
        [ordered]@{
            source_record_id = $sourceRecordId
            title = [string]$requiredValues["标题"]
            category_id = "119"
            category_name = "平板电脑"
            brand_id = $brandId
            brand_name = [string]$requiredValues["品牌"]
            model_id = $modelId
            model_name = [string]$requiredValues["型号"]
            content = [string]$requiredValues["综合内容"]
            source_fields = $sourceFields
        }
    )
}

if (-not $records.Count) {
    throw "飞书表格没有可同步的有效记录。"
}

$payload = [ordered]@{
    schema_version = 1
    source = "feishu_sheet"
    spreadsheet_token = $SpreadsheetToken
    sheet_id = $SheetId
    sheet_name = "个性化配置信息"
    revision = $response.data.revision
    category_id = "119"
    category_name = "平板电脑"
    records = $records
}

$resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutputPath)
if ($outputDirectory -and -not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}
$payload |
    ConvertTo-Json -Depth 20 |
    Set-Content -LiteralPath $resolvedOutputPath -Encoding utf8

[ordered]@{
    status = "success"
    output_path = $resolvedOutputPath
    records = $records.Count
    revision = $response.data.revision
} | ConvertTo-Json
