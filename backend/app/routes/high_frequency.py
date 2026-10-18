from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.routes.auth import require_permission
from app.services.high_frequency_pipeline import (
    build_persisted_dashboard,
    ingest_records,
)
from app.services.high_frequency import parse_source_record
from app.services.second_part_client import SecondPartClientError, fetch_records


router = APIRouter(prefix="/high-frequency", tags=["高频问题看板"])


def _shadow_comparison_path() -> Path:
    cache_path = Path(settings.HIGH_FREQUENCY_CLASSIFICATION_CACHE_PATH)
    return cache_path.parent / "high_frequency_shadow_comparison.json"


def _query_kwargs(
    from_date: str,
    to_date: str,
    category: str,
    level1: str,
    level2: str,
    business_theme: str,
    high_frequency_only: bool,
    period: str,
) -> dict[str, object]:
    return {
        "from_date": from_date,
        "to_date": to_date,
        "category": category,
        "level1": level1,
        "level2": level2,
        "business_theme": business_theme,
        "high_frequency_only": high_frequency_only,
        "period": period,
    }


def _assert_export_ready(payload: dict) -> None:
    meta = payload.get("meta", {})
    if meta.get("worker_state") == "failed" or meta.get("quality_status") == "failed":
        raise HTTPException(
            409,
            "存在 DeepSeek 处理失败的来源，不能作为最终报表导出。请在同步数据后重试失败任务。",
        )
    if meta.get("classification_pending") or meta.get("worker_state") == "running":
        raise HTTPException(
            409,
            "DeepSeek 高频问题增量处理尚未完成，请等待处理完成后再导出。",
        )
    if meta.get("possibly_truncated"):
        raise HTTPException(
            409,
            "当前日期范围的数据源可能达到返回上限，不能导出不完整的高频问题报表。请缩小日期范围后重新同步。",
        )


@router.get("")
def dashboard(
    from_date: str = Query(""),
    to_date: str = Query(""),
    category: str = Query(""),
    level1: str = Query(""),
    level2: str = Query(""),
    business_theme: str = Query(""),
    high_frequency_only: bool = Query(True),
    period: str = Query("week", pattern="^(week|month)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
):
    """Read only persisted source-to-standard-question mappings."""

    return build_persisted_dashboard(
        db,
        **_query_kwargs(
            from_date,
            to_date,
            category,
            level1,
            level2,
            business_theme,
            high_frequency_only,
            period,
        ),
    )


@router.post("/sync")
def sync(
    from_date: str = Query(...),
    to_date: str = Query(...),
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    """Fetch upstream once, persist versions, and queue only new deltas."""

    try:
        source_result = fetch_records(from_date, to_date)
    except SecondPartClientError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if dry_run:
        source_records = [
            parse_source_record(raw)
            for raw in source_result.records
            if isinstance(raw, dict)
        ]
        valid_records = [
            record for record in source_records if record.get("record_id")
        ]
        core_problem_records = [
            record for record in valid_records if record.get("core_problem")
        ]
        return {
            "dry_run": True,
            "from_date": from_date,
            "to_date": to_date,
            "fetched": len(source_result.records),
            "unique_record_count": len(
                {record["record_id"] for record in valid_records}
            ),
            "ai_core_problem_count": len(core_problem_records),
            "category_count": len(
                {
                    record["category"]
                    for record in core_problem_records
                    if record.get("category")
                }
            ),
            "possibly_truncated": source_result.possibly_truncated,
            "source_request_count": source_result.request_count,
            "message": "第二部分接口连接和返回字段校验通过；未写入数据库，未调用 DeepSeek。",
        }
    summary = ingest_records(
        db,
        source_result.records,
        source_possibly_truncated=source_result.possibly_truncated,
        source_request_count=source_result.request_count,
    )
    return {
        **summary,
        "from_date": from_date,
        "to_date": to_date,
        "possibly_truncated": source_result.possibly_truncated,
        "source_request_count": source_result.request_count,
        "message": (
            "已写入新增或变更工单，DeepSeek 将由后台增量处理。"
            if summary["queued"]
            else "本批工单内容未变化，未创建新的 DeepSeek 任务。"
        ),
    }


@router.get("/export")
def export(
    from_date: str = Query(""),
    to_date: str = Query(""),
    category: str = Query(""),
    level1: str = Query(""),
    level2: str = Query(""),
    business_theme: str = Query(""),
    high_frequency_only: bool = Query(True),
    period: str = Query("week", pattern="^(week|month)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
):
    payload = build_persisted_dashboard(
        db,
        **_query_kwargs(
            from_date,
            to_date,
            category,
            level1,
            level2,
            business_theme,
            high_frequency_only,
            period,
        ),
    )
    _assert_export_ready(payload)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "高频问题汇总"
    summary.append(
        [
            "排名",
            "品类",
            "业务主题",
            "标准问题",
            "出现次数",
            "出现占比",
            "工单数",
            "有话术",
            "统计周期",
            "最近出现时间",
        ]
    )
    for row in payload["rows"]:
        summary.append(
            [
                row["排名"],
                row["品类"],
                row["业务主题"],
                row["原子问题"],
                row["出现次数"],
                row["出现占比"],
                row["工单数"],
                row["有话术"],
                row["统计周期"],
                row["最近出现时间"],
            ]
        )
    detail = workbook.create_sheet("来源案例")
    detail.append(
        [
            "排名",
            "品类",
            "业务主题",
            "标准问题",
            "工单ID",
            "分析时间",
            "原始问题",
            "AI分析问题",
            "原始AI核心问题",
            "拆分状态",
            "判定结论",
            "判定依据",
            "参考话术",
        ]
    )
    for row in payload["rows"]:
        for case in row["案例"]:
            detail.append(
                [
                    row["排名"],
                    row["品类"],
                    row["业务主题"],
                    row["原子问题"],
                    case["工单ID"],
                    case["分析时间"],
                    case["原始问题"],
                    case["AI分析问题"],
                    case["原始AI核心问题"],
                    case["拆分状态"],
                    case["判定结论"],
                    case["判定依据"],
                    case["参考话术"],
                ]
            )
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = "高频问题看板.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


@router.get("/shadow-comparison")
def shadow_comparison(
    _: User = Depends(require_permission("knowledge:view")),
):
    """Return a local, read-only shadow comparison artifact when one exists."""

    path = _shadow_comparison_path()
    if not path.is_file():
        raise HTTPException(404, "暂无高频问题全量影子对比结果。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "高频问题影子对比结果文件不可读取。") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise HTTPException(500, "高频问题影子对比结果格式不受支持。")
    return payload
