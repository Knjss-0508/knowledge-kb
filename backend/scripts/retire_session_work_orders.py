"""一次性退池：把已判定为会话号的存量盲标工单退出派发。

背景（2026-10-14 线上事故 2110212947606246403）：建单门禁只能拦住「以后新建」的
工单，早先进池的会话号工单仍会被派发和标注 —— 当时池内 3535 条里有 285 条经访问
日志判定为会话号，其中 7 条在途、100 条已经被真实标注（782 条标注）。本脚本把
它们标成 ``pool_state='retired'`` 并释放在途派发，**不删除任何工单、派发或标注
记录**：``BlindLabelWorkOrder.assignments`` 是 ``cascade="all, delete-orphan"``，
物理删除会连带删掉已经完成的标注。

通常情况下不需要手工执行：判定 worker 每一轮都会自动退池，上限由
``WORK_ORDER_LOG_RETIRE_LIMIT``（默认 1000，一轮就足够消化 285 条存量）控制。
本脚本是给运维准备的可审计入口，默认 dry-run，必须显式 ``--apply`` 才写库。

用法（复用 kb-backend 容器里的数据库配置，与 ``scripts/audit/*.py`` 一致；
下面只把这个独立脚本拷进容器的 /tmp，不改镜像、不改容器里的应用代码）：

    docker cp backend/scripts/retire_session_work_orders.py kb-backend:/tmp/
    docker exec -e PYTHONPATH=/app kb-backend python /tmp/retire_session_work_orders.py
    docker exec -e PYTHONPATH=/app kb-backend python /tmp/retire_session_work_orders.py --apply

本机 checkout 直接跑（需要数据库可达，例如通过端口转发；不带 ``--apply`` 即 dry-run）：

    python backend/scripts/retire_session_work_orders.py
    python backend/scripts/retire_session_work_orders.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from sqlalchemy import func  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.blind_labeling import (  # noqa: E402
    POOL_STATE_RETIRED,
    BlindLabelAssignment,
    BlindLabelWorkOrder,
)
from app.services.blind_labeling import (  # noqa: E402
    _session_retire_candidates,
    retire_session_work_orders,
    session_number_snapshot,
)

DEFAULT_LIMIT = 1000
DEFAULT_MAX_ROUNDS = 200


def _pending(db, numbers, limit: int) -> tuple[int, int]:
    """本轮待退池工单数，以及其中仍在途（assigned/in_progress）的派发数。"""

    work_orders = _session_retire_candidates(db, numbers, limit)
    work_order_ids = [item.id for item in work_orders]
    if not work_order_ids:
        return 0, 0
    in_flight = (
        db.query(func.count(BlindLabelAssignment.id))
        .filter(
            BlindLabelAssignment.work_order_id.in_(work_order_ids),
            BlindLabelAssignment.status.in_(("assigned", "in_progress")),
        )
        .scalar()
        or 0
    )
    return len(work_order_ids), int(in_flight)


def _retired_summary(db) -> tuple[int, int]:
    """(已退池工单数, 这些工单上被保留下来的已完成派发数)。"""

    retired = (
        db.query(func.count(BlindLabelWorkOrder.id))
        .filter(BlindLabelWorkOrder.pool_state == POOL_STATE_RETIRED)
        .scalar()
        or 0
    )
    preserved = (
        db.query(func.count(BlindLabelAssignment.id))
        .join(
            BlindLabelWorkOrder,
            BlindLabelWorkOrder.id == BlindLabelAssignment.work_order_id,
        )
        .filter(
            BlindLabelWorkOrder.pool_state == POOL_STATE_RETIRED,
            BlindLabelAssignment.status == "completed",
        )
        .scalar()
        or 0
    )
    return int(retired), int(preserved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把已判定为会话号的存量盲标工单退池（默认 dry-run）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加这个参数就只统计（dry-run）",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="每轮最多处理多少条工单"
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="最多循环多少轮（防止意外的大规模改动）",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        numbers = session_number_snapshot()
        print(f"已判定为「只当过会话号」的号码：{len(numbers)} 个")
        pending_orders, pending_assignments = _pending(db, numbers, args.limit)
        print(
            f"本轮可退池工单：{pending_orders} 条"
            f"{'（达到 --limit，可能还有更多）' if pending_orders >= args.limit else ''}"
            f"，其中在途派发：{pending_assignments} 条"
        )
        if not args.apply:
            retired, preserved = _retired_summary(db)
            print(f"当前已退池工单：{retired} 条，保留下来的已完成派发：{preserved} 条")
            print("dry-run：没有写入任何数据。加 --apply 才会真正退池。")
            return 0
        total_retired = 0
        total_released = 0
        rounds = 0
        while rounds < max(1, args.max_rounds):
            result = retire_session_work_orders(db, limit=args.limit)
            db.commit()
            rounds += 1
            total_retired += int(result.get("retired") or 0)
            total_released += int(result.get("released") or 0)
            print(
                f"第 {rounds} 轮：退池 {result.get('retired')} 条，"
                f"释放在途派发 {result.get('released')} 条"
            )
            if not result.get("retired"):
                break
        retired, preserved = _retired_summary(db)
        print(
            f"完成：共退池 {total_retired} 条工单、释放 {total_released} 条在途派发"
            f"（{rounds} 轮）。当前已退池 {retired} 条，保留的已完成派发 {preserved} 条。"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
