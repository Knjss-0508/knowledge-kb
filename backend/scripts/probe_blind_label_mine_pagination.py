"""只读探针：用真实库核对「我的已标注」分页取数是否符合预期。

不使用任何写操作，只统计 completed 标注量并按页读取，用于验证
``list_my_assignments`` 返回的 items / total / total_pages 与真实数据一致。
"""

from __future__ import annotations

from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.blind_labeling import BlindLabelAssignment
from app.models.user import User
from app.routes.blind_labeling import list_my_assignments


def load_page(db, user, page: int, page_size: int) -> dict:
    """直接调用路由函数时必须显式传参，否则默认值仍是 FastAPI 的 Query 占位对象。"""
    return list_my_assignments(
        batch_id=None,
        assignment_status="completed",
        include_released=False,
        start_at=None,
        end_at=None,
        start_date=None,
        end_date=None,
        date_from=None,
        date_to=None,
        page=page,
        page_size=page_size,
        db=db,
        current_user=user,
    )


def main() -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(BlindLabelAssignment.user_id, func.count(BlindLabelAssignment.id))
            .filter(BlindLabelAssignment.status == "completed")
            .group_by(BlindLabelAssignment.user_id)
            .order_by(func.count(BlindLabelAssignment.id).desc())
            .limit(5)
            .all()
        )
        print("completed 标注量 TOP5 用户：", rows)
        if not rows:
            print("无 completed 标注数据，跳过")
            return
        user_id, expected_total = rows[0]
        user = db.get(User, user_id)
        if user is None:
            print(f"用户 {user_id} 不存在")
            return

        page_size = 200
        first = load_page(db, user, 1, page_size)
        print(
            f"用户 {user_id}: 首页 items={len(first['items'])} "
            f"total={first['total']} total_pages={first['total_pages']}"
        )

        total_pages = first["total_pages"]
        if total_pages < 2:
            print("该用户单页即可容纳，无法验证翻页")
        else:
            last = load_page(db, user, total_pages, page_size)
            seen = {item["id"] for item in first["items"]}
            overlap = seen & {item["id"] for item in last["items"]}
            print(
                f"末页 items={len(last['items'])} page={last['page']} "
                f"total={last['total']} 与首页重复={len(overlap)}"
            )
            beyond = load_page(db, user, total_pages + 1, page_size)
            print(f"越界页 items={len(beyond['items'])}（应为 0）")

        assert first["total"] == expected_total, (first["total"], expected_total)
        assert first["total_pages"] == max(1, -(-expected_total // page_size))
        print("OK: total / total_pages 与库内统计一致")
    finally:
        db.close()


if __name__ == "__main__":
    main()
