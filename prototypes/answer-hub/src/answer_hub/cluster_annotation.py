from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import csv
from io import StringIO
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


CLUSTER_DECISIONS = ("", "正确", "错误", "待定")
TITLE_DECISIONS = ("", "正确", "错误", "待定")
CLUSTER_ACTIONS = ("", "保留", "拆分", "合并", "转人工", "排除")


def _text(value: Any) -> str:
    return str(value or "").strip()


def split_media_urls(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    return list(
        dict.fromkeys(
            part.strip()
            for part in re.split(r"[\n,，;；\s]+", text)
            if part.strip().startswith(("http://", "https://"))
        )
    )


@dataclass(frozen=True)
class ClusterAnnotation:
    cluster_id: str
    cluster_decision: str = ""
    title_decision: str = ""
    action: str = ""
    outlier_atomic_ids: tuple[str, ...] = ()
    gold_topic_id: str = ""
    gold_title: str = ""
    target_cluster_id: str = ""
    notes: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outlier_atomic_ids"] = list(self.outlier_atomic_ids)
        return value


def load_cluster_payload(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"找不到完整聚类结果：{source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    clusters = payload.get("clusters")
    units = payload.get("atomic_units")
    if not isinstance(clusters, list) or not isinstance(units, list):
        raise ValueError("完整聚类结果缺少 clusters 或 atomic_units")

    unit_by_id = {
        _text(unit.get("unit_id")): unit
        for unit in units
        if isinstance(unit, dict) and _text(unit.get("unit_id"))
    }
    normalized_clusters: list[dict[str, Any]] = []
    seen_cluster_ids: set[str] = set()
    assigned_atomic_ids: set[str] = set()
    for source_cluster in clusters:
        if not isinstance(source_cluster, dict):
            continue
        cluster = dict(source_cluster)
        cluster_id = _text(cluster.get("cluster_id"))
        if not cluster_id or cluster_id in seen_cluster_ids:
            raise ValueError(f"主题簇ID为空或重复：{cluster_id or '空'}")
        seen_cluster_ids.add(cluster_id)
        member_ids = [
            _text(atomic_id)
            for atomic_id in cluster.get("member_atomic_ids") or []
            if _text(atomic_id)
        ]
        members = [
            unit_by_id[atomic_id]
            for atomic_id in member_ids
            if atomic_id in unit_by_id
        ]
        assigned_atomic_ids.update(member_ids)
        cluster["cluster_id"] = cluster_id
        cluster["member_atomic_ids"] = member_ids
        cluster["members"] = members
        cluster["member_count"] = len(members)
        cluster["theme_title"] = (
            _text(cluster.get("theme_title"))
            or _text(cluster.get("theme_name"))
            or cluster_id
        )
        cluster["requires_review"] = bool(
            cluster.get("title_status") == "error"
            or any(bool(member.get("requires_review")) for member in members)
        )
        normalized_clusters.append(cluster)

    normalized_clusters.sort(
        key=lambda cluster: (
            -int(cluster.get("member_count") or 0),
            not bool(cluster.get("requires_review")),
            cluster["cluster_id"],
        )
    )
    return {
        **payload,
        "clusters": normalized_clusters,
        "atomic_units": units,
        "unit_by_id": unit_by_id,
        "unclustered_atomic_ids": sorted(set(unit_by_id) - assigned_atomic_ids),
    }


class ClusterAnnotationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_annotations (
                    cluster_id TEXT PRIMARY KEY,
                    cluster_decision TEXT NOT NULL DEFAULT '',
                    title_decision TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    outlier_atomic_ids TEXT NOT NULL DEFAULT '[]',
                    gold_topic_id TEXT NOT NULL DEFAULT '',
                    gold_title TEXT NOT NULL DEFAULT '',
                    target_cluster_id TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    reviewer TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, cluster_id: str) -> ClusterAnnotation:
        normalized_id = _text(cluster_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_annotations WHERE cluster_id = ?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            return ClusterAnnotation(cluster_id=normalized_id)
        try:
            outlier_ids = tuple(json.loads(row["outlier_atomic_ids"]))
        except (json.JSONDecodeError, TypeError):
            outlier_ids = ()
        return ClusterAnnotation(
            cluster_id=row["cluster_id"],
            cluster_decision=row["cluster_decision"],
            title_decision=row["title_decision"],
            action=row["action"],
            outlier_atomic_ids=outlier_ids,
            gold_topic_id=row["gold_topic_id"],
            gold_title=row["gold_title"],
            target_cluster_id=row["target_cluster_id"],
            notes=row["notes"],
            reviewer=row["reviewer"],
            reviewed_at=row["reviewed_at"],
            updated_at=row["updated_at"],
        )

    def list_all(self) -> dict[str, ClusterAnnotation]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cluster_annotations ORDER BY cluster_id"
            ).fetchall()
        return {
            row["cluster_id"]: self.get(row["cluster_id"])
            for row in rows
        }

    def save(
        self,
        *,
        cluster_id: str,
        cluster_decision: str,
        title_decision: str,
        action: str,
        outlier_atomic_ids: list[str] | tuple[str, ...],
        gold_topic_id: str,
        gold_title: str,
        target_cluster_id: str,
        notes: str,
        reviewer: str,
    ) -> ClusterAnnotation:
        normalized_cluster_id = _text(cluster_id)
        if not normalized_cluster_id:
            raise ValueError("cluster_id不能为空")
        if cluster_decision not in CLUSTER_DECISIONS:
            raise ValueError(f"归簇判断不合法：{cluster_decision}")
        if title_decision not in TITLE_DECISIONS:
            raise ValueError(f"标题判断不合法：{title_decision}")
        if action not in CLUSTER_ACTIONS:
            raise ValueError(f"处理动作不合法：{action}")

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        existing = self.get(normalized_cluster_id)
        reviewed_at = existing.reviewed_at
        if (
            cluster_decision in {"正确", "错误"}
            or title_decision in {"正确", "错误"}
        ):
            reviewed_at = reviewed_at or now
        annotation = ClusterAnnotation(
            cluster_id=normalized_cluster_id,
            cluster_decision=cluster_decision,
            title_decision=title_decision,
            action=action,
            outlier_atomic_ids=tuple(
                dict.fromkeys(
                    _text(atomic_id)
                    for atomic_id in outlier_atomic_ids
                    if _text(atomic_id)
                )
            ),
            gold_topic_id=_text(gold_topic_id),
            gold_title=_text(gold_title),
            target_cluster_id=_text(target_cluster_id),
            notes=_text(notes),
            reviewer=_text(reviewer),
            reviewed_at=reviewed_at,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cluster_annotations (
                    cluster_id,
                    cluster_decision,
                    title_decision,
                    action,
                    outlier_atomic_ids,
                    gold_topic_id,
                    gold_title,
                    target_cluster_id,
                    notes,
                    reviewer,
                    reviewed_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cluster_id) DO UPDATE SET
                    cluster_decision = excluded.cluster_decision,
                    title_decision = excluded.title_decision,
                    action = excluded.action,
                    outlier_atomic_ids = excluded.outlier_atomic_ids,
                    gold_topic_id = excluded.gold_topic_id,
                    gold_title = excluded.gold_title,
                    target_cluster_id = excluded.target_cluster_id,
                    notes = excluded.notes,
                    reviewer = excluded.reviewer,
                    reviewed_at = excluded.reviewed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    annotation.cluster_id,
                    annotation.cluster_decision,
                    annotation.title_decision,
                    annotation.action,
                    json.dumps(
                        list(annotation.outlier_atomic_ids),
                        ensure_ascii=False,
                    ),
                    annotation.gold_topic_id,
                    annotation.gold_title,
                    annotation.target_cluster_id,
                    annotation.notes,
                    annotation.reviewer,
                    annotation.reviewed_at,
                    annotation.updated_at,
                ),
            )
        return annotation


def annotation_summary(
    clusters: list[dict[str, Any]],
    annotations: dict[str, ClusterAnnotation],
) -> dict[str, Any]:
    cluster_reviewed = [
        annotation
        for annotation in annotations.values()
        if annotation.cluster_decision in {"正确", "错误"}
    ]
    title_reviewed = [
        annotation
        for annotation in annotations.values()
        if annotation.title_decision in {"正确", "错误"}
    ]
    complete = [
        annotation
        for annotation in annotations.values()
        if annotation.cluster_decision in {"正确", "错误"}
        and annotation.title_decision in {"正确", "错误"}
    ]
    cluster_correct = sum(
        annotation.cluster_decision == "正确"
        for annotation in cluster_reviewed
    )
    title_correct = sum(
        annotation.title_decision == "正确"
        for annotation in title_reviewed
    )
    return {
        "cluster_count": len(clusters),
        "completed_count": len(complete),
        "pending_count": max(0, len(clusters) - len(complete)),
        "cluster_reviewed_count": len(cluster_reviewed),
        "cluster_correct_count": cluster_correct,
        "cluster_accuracy": (
            cluster_correct / len(cluster_reviewed)
            if cluster_reviewed
            else None
        ),
        "title_reviewed_count": len(title_reviewed),
        "title_correct_count": title_correct,
        "title_accuracy": (
            title_correct / len(title_reviewed)
            if title_reviewed
            else None
        ),
    }


def annotation_validation_errors(
    *,
    cluster_decision: str,
    title_decision: str,
    action: str,
    member_count: int,
    outlier_atomic_ids: list[str] | tuple[str, ...],
    gold_title: str,
    target_cluster_id: str,
) -> list[str]:
    errors: list[str] = []
    if cluster_decision == "错误" and action in {"", "保留"}:
        errors.append("归簇判断为“错误”时，请选择拆分、合并、转人工或排除。")
    if action == "拆分" and member_count > 1 and not outlier_atomic_ids:
        errors.append("选择“拆分”时，请勾选需要移出或单独拆分的成员。")
    if action == "合并" and not _text(target_cluster_id):
        errors.append("选择“合并”时，请选择目标主题簇。")
    if title_decision == "错误" and not _text(gold_title):
        errors.append("标题判断为“错误”时，请填写正确的人工主题标题。")
    return errors


def annotation_export_rows(
    clusters: list[dict[str, Any]],
    annotations: dict[str, ClusterAnnotation],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_id = _text(cluster.get("cluster_id"))
        annotation = annotations.get(
            cluster_id,
            ClusterAnnotation(cluster_id=cluster_id),
        )
        members = cluster.get("members") or []
        rows.append(
            {
                "模型簇ID": cluster_id,
                "模型主题标题": _text(cluster.get("theme_title")),
                "成员数": int(cluster.get("member_count") or 0),
                "产品类型": _text(cluster.get("product_category")),
                "成员原子ID": "\n".join(
                    _text(member.get("unit_id"))
                    for member in members
                ),
                "来源样本ID": "\n".join(
                    dict.fromkeys(
                        _text(member.get("sample_id"))
                        for member in members
                    )
                ),
                "人工归簇判断": annotation.cluster_decision,
                "人工标题判断": annotation.title_decision,
                "处理动作": annotation.action,
                "需移出原子ID": "\n".join(annotation.outlier_atomic_ids),
                "人工主题ID": annotation.gold_topic_id,
                "人工主题标题": annotation.gold_title,
                "目标模型簇ID": annotation.target_cluster_id,
                "人工备注": annotation.notes,
                "审核人": annotation.reviewer,
                "审核时间": annotation.reviewed_at,
                "更新时间": annotation.updated_at,
            }
        )
    return rows


def annotation_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
