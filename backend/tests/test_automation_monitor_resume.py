from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import auth, automation_monitor


@pytest.mark.parametrize(
    ("role", "resume_status"),
    [
        ("super_admin", 200),
        ("super_support", 200),
        ("senior_support", 403),
        ("junior_support", 403),
        ("visitor", 403),
    ],
)
def test_resume_uses_a_scoped_permission_without_unlocking_other_controls(
    monkeypatch, role, resume_status
):
    app = FastAPI()
    app.include_router(automation_monitor.router)
    app.dependency_overrides[auth.get_current_user] = lambda: SimpleNamespace(role=role)
    gateway = Mock()
    gateway.request.return_value = {"status": "resumed"}
    monkeypatch.setattr(automation_monitor, "_gateway", lambda: gateway)
    client = TestClient(app)

    response = client.post("/automation-monitor/resume")
    assert response.status_code == resume_status
    if resume_status == 200:
        gateway.request.assert_called_once_with("POST", "/api/v1/automation/resume")
    else:
        gateway.request.assert_not_called()

    if role != "super_admin":
        gateway.reset_mock()
        assert client.post("/automation-monitor/runs").status_code == 403
        assert client.post("/automation-monitor/retry-failed").status_code == 403
        assert client.patch("/automation-monitor/control", json={"enabled": False}).status_code == 403
        gateway.request.assert_not_called()
