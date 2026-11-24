"""Synthetic acceptance fixture. Writes only to a fresh named smoke database."""

import json
import math
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base
from app.models.blind_labeling import BlindLabelAnnotation, BlindLabelAssignment, BlindLabelBatch, BlindLabelWorkOrder
from app.models.knowledge import Category, Knowledge, KnowledgeSearchEmbedding, KnowledgeStatus
from app.models.user import User
from app.schemas.blind_labeling import BlindLabelLearningReviewInput
from app.services.blind_label_learning import load_learning_review, save_learning_review
from app.services.knowledge_dedup import _query_embedding
from scripts._blind_replay_io import local_database
from scripts.replay_blind_label_retrieval import _local_endpoints


def _fixture_vector(query: list[float], cosine: float) -> list[float]:
    """Make repeatable score differences without pretending to estimate model quality."""
    norm = math.sqrt(sum(x * x for x in query))
    unit = [x / norm for x in query]
    pivot = min(range(len(unit)), key=lambda i: abs(unit[i]))
    proj = unit[pivot]
    residual = [-proj * x for x in unit]
    residual[pivot] += 1
    scaling = math.sqrt((1 - cosine**2) / (1 - proj**2))
    return [cosine * a + scaling * b for a, b in zip(unit, residual)]


def main() -> None:
    # Imports the model registry used by the running API.
    from app import main as _main  # noqa: F401

    database = local_database()
    if (database.host, database.port, database.database, database.username) != (
        "127.0.0.1", 55432, "blind_cycle_smoke_only", "cycle_smoke_admin",
    ) or database.password:
        raise RuntimeError("refusing to seed a non-smoke database")
    settings.EMBEDDING_BASE_URL = _local_endpoints()[1]
    settings.EMBEDDING_API_KEY = ""
    # Check service before touching the disposable DB.
    _query_embedding("合成验收启动检查")
    engine = create_engine(database, hide_parameters=True)
    with engine.begin() as conn:
        existing = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
        if existing and (
            existing != set(Base.metadata.tables)
            or conn.execute(text("SELECT EXISTS (SELECT FROM blind_label_work_orders) OR "
                                 "EXISTS (SELECT FROM knowledge_items)")).scalar_one()
        ):
            raise RuntimeError("smoke database must be empty")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(conn)
    retrieval, answer = [], []
    with Session(engine) as db:
        db.add(Category(id="smoke-category", name="合成验收分类"))
        db.add_all(User(id=f"fixture-user-{v}", username=f"fixture-user-{v}", password_hash="unused")
                   for v in range(5))
        db.flush()
        db.add_all(BlindLabelBatch(id=f"fixture-batch-{v}", user_id=f"fixture-user-{v}")
                   for v in range(5))
        db.flush()
        for i in range(30):
            scope = f"smoke-scope-{i:02d}"
            question = f"虚构试验设备 R{i:02d} 的红色拨杆如何归位？"
            vector = _query_embedding(question)
            articles = []
            # Positive starts at rank four. Distances intentionally exercise the learned reranker.
            for j in range(4):
                title = "不适用的灰色支架维护" if j < 3 else question
                article = Knowledge(
                    id=f"fixture-k-{i:02d}-{j}", title=title, content={"text": "仅供合成验收。"},
                    knowledge_origin="business_accumulation", business_type="self_operated",
                    category_id="smoke-category", applicable_categories=[scope],
                    status=KnowledgeStatus.PUBLISHED, created_by="fixture",
                )
                articles.append(article)
                db.add(article)
            db.flush()
            for j, (article, cosine) in enumerate(zip(articles, (0.82, 0.81, 0.80, 0.79))):
                db.add(KnowledgeSearchEmbedding(
                    id=f"fixture-vec-{i:02d}-{j}", knowledge_id=article.id, embedding_model=settings.EMBEDDING_MODEL,
                    embedding_kind="content", content_hash=f"fixture-{i}-{j}", source_text="synthetic fixture",
                    embedding_dimension=len(vector), embedding=[], embedding_vector=_fixture_vector(vector, cosine),
                ))
            ordered = [articles[0], articles[1], articles[3]]
            order = f"fixture-w-{i:02d}"
            _make_order(db, order, question, ordered)
            review_id = _review(db, order, "ranking", articles[3].id)
            retrieval.append({"work_order_id": order, "review_id": review_id, "request": {
                "normalizedQuestion": question, "businessType": "self_operated", "categoryId": scope, "limit": 5,
            }})
            if i == 0:
                for suffix, issue in (("gap", "knowledge_gap"), ("answer", "answer_issue"), ("pending", None)):
                    case_id = f"fixture-a-{suffix}"
                    _make_order(db, case_id, question, ordered)
                    if issue == "knowledge_gap":
                        _review(db, case_id, issue)
                    elif issue == "answer_issue":
                        answer.append({
                            "work_order_id": case_id,
                            "review_id": _review(db, case_id, issue, articles[3].id, "模拟：先断电再归位。"),
                            "baseline": {"generation": "answered", "answer": "模拟：直接归位。", "verdict": "incorrect"},
                            "candidate": {"generation": "answered", "answer": "模拟：先断电再归位。", "verdict": "correct"},
                        })
        db.commit()
        conn = db.connection()
        conn.execute(text("CREATE ROLE cycle_smoke_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"))
        conn.execute(text("ALTER ROLE cycle_smoke_ro SET default_transaction_read_only TO on"))
        conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO cycle_smoke_ro"))
        db.commit()
    output = Path(sys.argv[1])
    for name, data in (
        ("synthetic-retrieval.json", {"cases": retrieval}),
        ("synthetic-answer.json", {"cases": answer, "baseline_version": "fixture-baseline", "candidate_version": "fixture-candidate"}),
    ):
        with (output / name).open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True)
    print("synthetic fixture: 30 retrieval cases, 1 answer pair, 1 editor case, 1 AI-only case")


def _make_order(db, oid, question, articles):
    db.add(BlindLabelWorkOrder(
        id=oid, conversation_id=oid, query_text=question, category_id="smoke-category",
        candidate_snapshot=[{
            "candidate_ref": f"c{j}", "rank": j + 1, "knowledge_id": article.id,
            "title": article.title, "content": article.content,
        } for j, article in enumerate(articles)],
    ))
    db.flush()
    for v in range(5):
        aid = f"{oid}-{v}"
        db.add(BlindLabelAssignment(
            id=aid, user_id=f"fixture-user-{v}", batch_id=f"fixture-batch-{v}", work_order_id=oid, status="completed",
        ))
        db.flush()
        db.add_all(BlindLabelAnnotation(
            id=f"{aid}-{j}", assignment_id=aid, user_id=f"fixture-user-{v}",
            work_order_id=oid, candidate_ref=f"c{j}",
            verdict="referable" if j == 2 else "not_referable",
        ) for j in range(3))
    db.flush()


def _review(db, oid, issue, knowledge=None, answer=""):
    detail = load_learning_review(db, oid)
    result = save_learning_review(db, oid, BlindLabelLearningReviewInput(
        evidence_version=detail["sample"]["evidence_version"], previous_review_id=None,
        issue_type=issue, knowledge_id=knowledge, expected_answer=answer,
    ), reviewer_id="synthetic-fixture-review")
    return result["review"]["id"]


if __name__ == "__main__":
    main()
