"""访问日志工单号判定的单元测试（无需 Cookie、不访问网络）。"""

from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.integration import RetrievalQualityEvent
from app.services import work_order_log_verdicts as verdicts


def _line(url: str, size: int, status: int = 200) -> str:
    return (
        '10.0.0.1 - - [09/Oct/2026:20:34:03 +0800] "GET %s HTTP/1.1" %s %s '
        '"https://zzdy.powerzhuan.cn/" "Mozilla/5.0"\n' % (url, status, size)
    )


def _detail(number: str, size: int) -> str:
    return _line("/nmhtapi/qa/queryQuestionFormDetail?questionFormId=%s&sceneType=2" % number, size)


def _chat(number: str, size: int) -> str:
    return _line("/nmhtapi/im/history?conversationId=%s" % number, size)


def test_classify_log_line_reads_response_sizes():
    assert verdicts.classify_log_line(_detail("2108802993527718918", 639)) == (
        "2108802993527718918",
        verdicts.SIGNAL_DETAIL_PRESENT,
    )
    assert verdicts.classify_log_line(_detail("2108536379561477121", 124)) == (
        "2108536379561477121",
        verdicts.SIGNAL_DETAIL_MISSING,
    )
    assert verdicts.classify_log_line(_chat("2108536379561477121", 827)) == (
        "2108536379561477121",
        verdicts.SIGNAL_CHAT_PRESENT,
    )
    # 空会话（无聊天内容）不能当证据；中间体量（128~300）也不下结论。
    assert verdicts.classify_log_line(_chat("2108536379561477121", 68)) is None
    assert verdicts.classify_log_line(_detail("2108802993527718918", 200)) is None
    assert verdicts.classify_log_line(_line("/app", 1234)) is None
    assert verdicts.classify_log_line('broken "GET /x' + ' HTTP/1.1" 200 100') is None


def test_apply_signal_keeps_a_real_work_order_real():
    store: dict[str, str] = {}
    assert verdicts.apply_signal(store, "111", verdicts.SIGNAL_CHAT_PRESENT) is True
    assert store["111"] == verdicts.VERDICT_SESSION
    assert verdicts.apply_signal(store, "111", verdicts.SIGNAL_CHAT_PRESENT) is False
    assert verdicts.apply_signal(store, "111", verdicts.SIGNAL_DETAIL_MISSING) is False
    assert store["111"] == verdicts.VERDICT_SESSION
    assert verdicts.apply_signal(store, "111", verdicts.SIGNAL_DETAIL_PRESENT) is True
    assert store["111"] == verdicts.VERDICT_REAL
    # 真工单号即使也被当作会话号用，也不能降级。
    assert verdicts.apply_signal(store, "111", verdicts.SIGNAL_CHAT_PRESENT) is False
    assert store["111"] == verdicts.VERDICT_REAL
    # 只有「查不到」不下结论。
    assert verdicts.apply_signal(store, "222", verdicts.SIGNAL_DETAIL_MISSING) is False
    assert "222" not in store


def test_scan_log_is_incremental_and_persists_verdicts(tmp_path):
    verdicts.reset_cache()
    log = tmp_path / "access.log"
    state = tmp_path / "state.json"
    log.write_text(
        _detail("2108802993527718918", 639) + _chat("2108536379561477121", 827),
        encoding="utf-8",
    )
    stats = verdicts.scan_log(path=str(log), state_file=str(state))
    assert stats["new_real"] == 1
    assert stats["new_session"] == 1
    assert stats["scanned_lines"] == 2
    assert stats["offset"] == log.stat().st_size
    assert stats["changed"] == {
        "2108802993527718918": verdicts.VERDICT_REAL,
        "2108536379561477121": verdicts.VERDICT_SESSION,
    }
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["offset"] == log.stat().st_size
    assert payload["verdicts"] == stats["changed"]
    assert verdicts.verdict_for("2108802993527718918", state_file=str(state)) == verdicts.VERDICT_REAL
    assert verdicts.verdict_for("2108536379561477121", state_file=str(state)) == verdicts.VERDICT_SESSION
    assert verdicts.verdict_for("1", state_file=str(state)) is None

    with open(log, "a", encoding="utf-8") as handle:
        handle.write(_chat("2108086877172007936", 520))
    second = verdicts.scan_log(path=str(log), state_file=str(state))
    # 只消费新增的那一行，历史结论不重算。
    assert second["scanned_lines"] == 1
    assert second["new_real"] == 0
    assert second["new_session"] == 1
    assert second["changed"] == {"2108086877172007936": verdicts.VERDICT_SESSION}
    assert verdicts.verdict_for("2108086877172007936", state_file=str(state)) == verdicts.VERDICT_SESSION
    assert verdicts.verdict_for("2108802993527718918", state_file=str(state)) == verdicts.VERDICT_REAL


def test_partial_last_line_waits_for_its_newline(tmp_path):
    verdicts.reset_cache()
    log = tmp_path / "access.log"
    state = tmp_path / "state.json"
    log.write_text(_detail("777", 700), encoding="utf-8")
    log.write_text(
        log.read_text(encoding="utf-8") + _detail("888", 700).rstrip("\n"), encoding="utf-8"
    )
    stats = verdicts.scan_log(path=str(log), state_file=str(state))
    assert stats["offset"] < log.stat().st_size
    assert verdicts.verdict_for("777", state_file=str(state)) == verdicts.VERDICT_REAL
    assert verdicts.verdict_for("888", state_file=str(state)) is None

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\n")
    verdicts.scan_log(path=str(log), state_file=str(state))
    assert verdicts.verdict_for("888", state_file=str(state)) == verdicts.VERDICT_REAL


def test_truncated_log_is_rescanned_without_losing_verdicts(tmp_path):
    verdicts.reset_cache()
    log = tmp_path / "access.log"
    state = tmp_path / "state.json"
    log.write_text(_detail("111", 639) + _chat("222", 827), encoding="utf-8")
    verdicts.scan_log(path=str(log), state_file=str(state))

    log.write_text(_detail("333", 639), encoding="utf-8")
    stats = verdicts.scan_log(path=str(log), state_file=str(state))
    assert stats["rotation_reset"] is True
    assert stats["new_real"] == 1
    assert verdicts.verdict_for("111", state_file=str(state)) == verdicts.VERDICT_REAL
    assert verdicts.verdict_for("222", state_file=str(state)) == verdicts.VERDICT_SESSION
    assert verdicts.verdict_for("333", state_file=str(state)) == verdicts.VERDICT_REAL


def test_scan_log_reports_a_missing_log_without_failing(tmp_path):
    stats = verdicts.scan_log(path=str(tmp_path / "nope.log"), state_file=str(tmp_path / "s.json"))
    assert stats["missing"] is True
    assert stats["offset"] == 0
    assert stats["new_real"] == 0


def test_scan_log_honours_the_per_run_byte_budget(tmp_path):
    verdicts.reset_cache()
    log = tmp_path / "access.log"
    state = tmp_path / "state.json"
    first = _detail("111", 639)
    log.write_text(first + _detail("222", 639), encoding="utf-8")
    stats = verdicts.scan_log(path=str(log), state_file=str(state), max_bytes=len(first) + 1)
    assert stats["offset"] < log.stat().st_size
    assert verdicts.verdict_for("111", state_file=str(state)) == verdicts.VERDICT_REAL
    assert verdicts.verdict_for("222", state_file=str(state)) is None
    verdicts.scan_log(path=str(log), state_file=str(state))
    assert verdicts.verdict_for("222", state_file=str(state)) == verdicts.VERDICT_REAL


def test_verdict_stats_reports_counts(tmp_path):
    verdicts.reset_cache()
    log = tmp_path / "access.log"
    state = tmp_path / "state.json"
    log.write_text(_detail("111", 639) + _chat("222", 827), encoding="utf-8")
    verdicts.scan_log(path=str(log), state_file=str(state))
    stats = verdicts.verdict_stats(state_file=str(state))
    assert stats["counts"] == {verdicts.VERDICT_REAL: 1, verdicts.VERDICT_SESSION: 1}
    assert stats["tracked"] == 2
    assert stats["updated_at"]
    assert stats["offset"] == log.stat().st_size


def test_backfill_verdicts_marks_and_unblocks_telemetry_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    RetrievalQualityEvent.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(
        RetrievalQualityEvent(
            id="rqe-1",
            idempotency_key="k1",
            source_system="knowledge-kb-standard-search",
            conversation_id="c1",
            question_form_id="111",
            query_text="q1",
            score_threshold=0.5,
            outcome="answered",
        )
    )
    session.add(
        RetrievalQualityEvent(
            id="rqe-2",
            idempotency_key="k2",
            source_system="knowledge-kb-standard-search",
            conversation_id="c2",
            question_form_id="222",
            work_order_verified=True,
            query_text="q2",
            score_threshold=0.5,
            outcome="answered",
        )
    )
    session.commit()
    session.close()

    updated = verdicts.backfill_verdicts(
        {"111": verdicts.VERDICT_SESSION, "222": verdicts.VERDICT_SESSION},
        session_factory=session_factory,
    )
    assert updated == 2
    session = session_factory()
    assert session.get(RetrievalQualityEvent, "rqe-1").work_order_verified is False
    assert session.get(RetrievalQualityEvent, "rqe-2").work_order_verified is False
    session.close()

    # 号码后来被证实是真工单号时必须能解除拦截。
    updated = verdicts.backfill_verdicts({"111": verdicts.VERDICT_REAL}, session_factory=session_factory)
    assert updated == 1
    session = session_factory()
    assert session.get(RetrievalQualityEvent, "rqe-1").work_order_verified is True
    assert session.get(RetrievalQualityEvent, "rqe-2").work_order_verified is False
    session.close()

    # 非法号码与空结论不写库。
    assert verdicts.backfill_verdicts({"abc": verdicts.VERDICT_SESSION}, session_factory=session_factory) == 0
    assert verdicts.backfill_verdicts({}, session_factory=session_factory) == 0


def test_backfill_verdicts_writes_whole_batches_through_a_small_chunk_size():
    """一次全量扫描会判定上万条号码，回写必须分批而不是只处理前 N 条。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    RetrievalQualityEvent.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    for index in range(5):
        session.add(
            RetrievalQualityEvent(
                id="rqe-%s" % index,
                idempotency_key="k%s" % index,
                source_system="knowledge-kb-standard-search",
                conversation_id="c%s" % index,
                question_form_id=str(1000 + index),
                query_text="q",
                score_threshold=0.5,
                outcome="answered",
            )
        )
    session.commit()
    session.close()

    changed = {str(1000 + index): verdicts.VERDICT_SESSION for index in range(5)}
    assert verdicts.backfill_verdicts(changed, session_factory=session_factory, chunk_size=2) == 5
    session = session_factory()
    marked = [session.get(RetrievalQualityEvent, "rqe-%s" % index).work_order_verified for index in range(5)]
    session.close()
    assert marked == [False] * 5


def test_backfill_all_verdicts_drains_the_state_file_and_marks_it(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    RetrievalQualityEvent.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(
        RetrievalQualityEvent(
            id="rqe-old",
            idempotency_key="k-old",
            source_system="knowledge-kb-standard-search",
            conversation_id="c-old",
            question_form_id="2108536379561477121",
            query_text="q",
            score_threshold=0.5,
            outcome="answered",
        )
    )
    session.commit()
    session.close()

    state_file = str(tmp_path / "state.json")
    verdicts._write_state(
        state_file,
        {
            "offset": 10,
            "inode": 1,
            "size": 10,
            "verdicts": {"2108536379561477121": verdicts.VERDICT_SESSION},
            "backfilled_at": "",
        },
    )
    assert verdicts.state_needs_backfill(state_file=state_file)
    assert verdicts.backfill_all_verdicts(state_file=state_file, session_factory=session_factory, chunk_size=1) == 1

    session = session_factory()
    assert session.get(RetrievalQualityEvent, "rqe-old").work_order_verified is False
    session.close()

    verdicts.mark_backfilled(state_file=state_file)
    payload = json.loads(open(state_file, encoding="utf-8").read())
    assert payload["backfilled_at"]
    assert not verdicts.state_needs_backfill(state_file=state_file)

