import unittest
from pathlib import Path


class AnswerHubOperationsContractTests(unittest.TestCase):
    def test_answer_hub_operations_routes_are_exposed_with_intended_permissions(self):
        backend_root = Path(__file__).resolve().parents[1]
        route_source = (backend_root / "app" / "routes" / "answer_hub.py").read_text(
            encoding="utf-8"
        )
        main_source = (backend_root / "app" / "main.py").read_text(encoding="utf-8")

        self.assertIn('@router.get("/overview")', route_source)
        self.assertIn('@router.patch("/control")', route_source)
        self.assertIn('@router.post("/runs")', route_source)
        self.assertIn('@router.post("/jobs/{job_id}/retry")', route_source)
        self.assertIn('require_permission("knowledge:submit")', route_source)
        self.assertIn('require_permission("account:manage")', route_source)
        self.assertIn("app.include_router(answer_hub.router", main_source)

    def test_answer_hub_payload_sanitizer_hides_paths_and_secrets(self):
        from app.services.answer_hub_operations import sanitize_answer_hub_payload

        payload = {
            "error": "failed at C:\\private\\run.json?token=should-not-leak",
            "metadata": {"secret": "should-not-leak"},
            "items": [{"job_id": "job-1", "status_url": "http://internal/private"}],
        }

        safe = sanitize_answer_hub_payload(payload)
        rendered = str(safe)

        self.assertEqual(safe["items"][0]["job_id"], "job-1")
        self.assertNotIn("private", rendered)
        self.assertNotIn("should-not-leak", rendered)
        self.assertNotIn("metadata", safe)
        self.assertNotIn("status_url", safe["items"][0])

    def test_browser_uses_only_the_cz_answer_hub_gateway(self):
        project_root = Path(__file__).resolve().parents[2]
        frontend = (project_root / "frontend" / "index.html").read_text(encoding="utf-8")

        self.assertIn("/answer-hub/overview", frontend)
        self.assertIn("/answer-hub/control", frontend)
        self.assertIn("/answer-hub/runs", frontend)
        self.assertNotIn("X-Answer-Hub-Key", frontend)
        self.assertNotIn("ANSWER_HUB_API_KEY", frontend)
        self.assertNotIn("/api/v1/automation", frontend)

    def test_compose_passes_answer_hub_configuration_to_the_backend(self):
        project_root = Path(__file__).resolve().parents[2]
        compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("ANSWER_HUB_BASE_URL: ${ANSWER_HUB_BASE_URL:-}", compose)
        self.assertIn("ANSWER_HUB_API_KEY: ${ANSWER_HUB_API_KEY:-}", compose)
        self.assertIn(
            "ANSWER_HUB_TIMEOUT_SECONDS: ${ANSWER_HUB_TIMEOUT_SECONDS:-10}",
            compose,
        )


if __name__ == "__main__":
    unittest.main()
