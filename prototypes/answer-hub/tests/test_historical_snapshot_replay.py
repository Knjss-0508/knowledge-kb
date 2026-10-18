import importlib.util
import json
from pathlib import Path
import socket
import sys

import pytest


@pytest.mark.parametrize("missing_field", [
    "status", "version", "source_file", "source_sheet", "source_rows",
])
def test_historical_replay_rejects_incomplete_explicit_provenance(
    tmp_path, monkeypatch, missing_field,
):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "historical_replay_test", scripts / "replay_personal_frozen_topics.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "model_profile", lambda _: ("test", "https://example.invalid", "test"))
    monkeypatch.setattr(
        socket.socket, "connect",
        lambda *a, **k: pytest.fail("invalid snapshot must fail before network access"),
    )
    item = {
        "standard_id": "TEST-HISTORICAL", "status": "published", "version": "test-v1",
        "source_file": "test.xlsx", "source_sheet": "test", "source_rows": [3],
    }
    del item[missing_field]
    snapshot = tmp_path / "historical.json"
    snapshot.write_text(json.dumps({
        "compiled_at": "2026-08-18", "version": "test-v1", "items": [item],
    }), encoding="utf-8")
    output = tmp_path / "new-output"
    monkeypatch.setattr(sys, "argv", [
        "replay", "--groups", str(tmp_path / "unused.json"),
        "--source-rows", "1", "--output-root", str(output),
        "--historical-standards-snapshot", str(snapshot),
    ])
    with pytest.raises(ValueError, match="explicit published status and source provenance"):
        module.main()
    assert not output.exists()
