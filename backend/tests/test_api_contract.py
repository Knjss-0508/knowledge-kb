import json
import unittest

from app.main import app


class ApiContractTests(unittest.TestCase):
    def test_excel_import_routes_are_exposed(self):
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/knowledge/import/template", paths)
        self.assertIn("/api/v1/knowledge/import/excel", paths)

    def test_knowledge_list_exposes_applicability_filters(self):
        operation = app.openapi()["paths"]["/api/v1/knowledge"]["get"]
        parameter_names = {
            parameter["name"] for parameter in operation["parameters"]
        }
        self.assertTrue(
            {
                "applicable_category_ids",
                "brand_ids",
                "model_ids",
            }.issubset(parameter_names)
        )

    def test_candidate_review_routes_are_exposed(self):
        paths = app.openapi()["paths"]

        self.assertIn(
            "post",
            paths["/api/v1/integration/knowledge-review-candidates:batch"],
        )
        self.assertIn("get", paths["/api/v1/integration/candidate-reviews"])
        self.assertIn(
            "patch",
            paths["/api/v1/integration/candidate-reviews/{ingestion_id}"],
        )
        self.assertIn(
            "post",
            paths["/api/v1/integration/candidate-reviews:batch-submit"],
        )

    def test_integration_processing_exposes_plugin_contract(self):
        schemas = app.openapi()["components"]["schemas"]
        processing = schemas["IntegrationProcessing"]["properties"]

        self.assertIn("plugin_name", processing)
        self.assertIn("plugin_version", processing)

    def test_openapi_no_longer_exposes_knowledge_layer(self):
        specification = json.dumps(app.openapi(), ensure_ascii=False)
        self.assertNotIn('"layer"', specification)
        self.assertNotIn("知识层级", specification)
        self.assertNotIn("applicable_business_types", specification)
        self.assertNotIn("is_model_personal", specification)
        self.assertNotIn("适用业务", specification)
        self.assertNotIn("机型个性化", specification)


if __name__ == "__main__":
    unittest.main()
