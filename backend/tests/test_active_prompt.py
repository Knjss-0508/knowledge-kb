from datetime import datetime
from types import SimpleNamespace

from app.services import active_prompt


class _Query:
    def __init__(self, jobs):
        self.jobs = jobs

    def order_by(self, *_args):
        return self

    def all(self):
        return self.jobs


class _Db:
    def __init__(self, jobs):
        self.jobs = jobs

    def query(self, *_args):
        return _Query(self.jobs)

    def close(self):
        pass


def _approved_job(*, target="review", approved_at="2026-10-10T12:00:00"):
    return SimpleNamespace(
        analysis_result={"optimization_target": target},
        prompt_versions=[{
            "version": "candidate-v3",
            "prompt": "仅用于后续新候选的 Prompt",
            "status": "approved_for_future_candidates",
            "approved_at": approved_at,
        }],
    )


def test_human_approval_becomes_the_active_prompt_for_new_work(monkeypatch):
    db = _Db([_approved_job()])
    monkeypatch.setattr(active_prompt, "SessionLocal", lambda: db)

    prompt, version = active_prompt.get_active_confidence_prompt()

    assert prompt == "仅用于后续新候选的 Prompt"
    assert version == "candidate-v3"


def test_approved_prompt_is_scoped_to_candidates_created_after_approval(monkeypatch):
    db = _Db([_approved_job()])
    before = active_prompt.get_confidence_prompt_for_candidate(
        datetime(2026, 10, 10, 11, 59),
        db=db,
    )
    after = active_prompt.get_confidence_prompt_for_candidate(
        datetime(2026, 10, 10, 12, 0),
        db=db,
    )

    assert before == (
        active_prompt.DEFAULT_PROMPT,
        active_prompt.DEFAULT_PROMPT_VERSION,
    )
    assert after == ("仅用于后续新候选的 Prompt", "candidate-v3")


def test_draft_generation_approval_is_not_used_for_candidate_review(monkeypatch):
    db = _Db([_approved_job(target="draft_generation")])
    selected = active_prompt.get_confidence_prompt_for_candidate(
        datetime(2026, 10, 11),
        db=db,
    )

    assert selected == (
        active_prompt.DEFAULT_PROMPT,
        active_prompt.DEFAULT_PROMPT_VERSION,
    )


def test_candidate_prompt_is_frozen_by_creation_time_across_two_approvals():
    v2 = _approved_job(approved_at="2026-10-10T12:00:00")
    v2.prompt_versions[0].update(
        {"version": "v2-active", "prompt": "Prompt v2"}
    )
    v3 = _approved_job(approved_at="2026-10-12T12:00:00")
    v3.prompt_versions[0].update(
        {"version": "v3-active", "prompt": "Prompt v3"}
    )
    db = _Db([v3, v2])

    old_candidate = active_prompt.get_confidence_prompt_for_candidate(
        datetime(2026, 10, 11),
        db=db,
    )
    new_candidate = active_prompt.get_confidence_prompt_for_candidate(
        datetime(2026, 10, 13),
        db=db,
    )
    active = active_prompt.get_active_confidence_prompt(db=db)

    assert old_candidate == ("Prompt v2", "v2-active")
    assert new_candidate == ("Prompt v3", "v3-active")
    assert active == ("Prompt v3", "v3-active")
