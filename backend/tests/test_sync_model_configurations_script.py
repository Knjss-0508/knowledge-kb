import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.scripts import sync_model_configurations as script
from app.services.model_configuration import ModelConfigurationSyncError


def _payload() -> dict:
    return {
        "records": [
            {
                "source_record_id": "56383",
                "title": "iPad 10 机型配置",
                "category_id": "119",
                "category_name": "平板电脑",
                "brand_id": "10530",
                "brand_name": "苹果",
                "model_id": "97519",
                "model_name": "iPad 10 (2022) 10.9英寸",
                "content": "Home键：支持；",
            }
        ]
    }


class SyncModelConfigurationsScriptTests(unittest.TestCase):
    def test_invalid_json_returns_structured_sync_error(self):
        with (
            patch.object(
                script,
                "_parse_args",
                return_value=SimpleNamespace(input="-", actor="sync"),
            ),
            patch.object(script.sys, "stdin", io.StringIO("{")),
        ):
            with self.assertRaises(ModelConfigurationSyncError) as raised:
                script.main()

        self.assertEqual(
            raised.exception.code,
            "MODEL_CONFIGURATION_JSON_INVALID",
        )

    def test_payload_root_must_be_object(self):
        with (
            patch.object(
                script,
                "_parse_args",
                return_value=SimpleNamespace(input="-", actor="sync"),
            ),
            patch.object(script.sys, "stdin", io.StringIO("[]")),
        ):
            with self.assertRaises(ModelConfigurationSyncError) as raised:
                script.main()

        self.assertEqual(
            raised.exception.code,
            "MODEL_CONFIGURATION_PAYLOAD_INVALID",
        )

    def test_actor_must_fit_database_column(self):
        with (
            patch.object(
                script,
                "_parse_args",
                return_value=SimpleNamespace(input="-", actor="a" * 129),
            ),
            patch.object(
                script.sys,
                "stdin",
                io.StringIO(json.dumps(_payload(), ensure_ascii=False)),
            ),
        ):
            with self.assertRaises(ModelConfigurationSyncError) as raised:
                script.main()

        self.assertEqual(
            raised.exception.code,
            "MODEL_CONFIGURATION_ACTOR_TOO_LONG",
        )

    def test_valid_payload_commits_and_closes_session(self):
        db = MagicMock()
        result = SimpleNamespace(
            total=1,
            created=1,
            updated=0,
            unchanged=0,
        )
        with (
            patch.object(
                script,
                "_parse_args",
                return_value=SimpleNamespace(input="-", actor=" sync "),
            ),
            patch.object(
                script.sys,
                "stdin",
                io.StringIO(json.dumps(_payload(), ensure_ascii=False)),
            ),
            patch.object(script, "SessionLocal", return_value=db),
            patch.object(
                script,
                "sync_model_configurations",
                return_value=result,
            ) as sync,
            patch("builtins.print"),
        ):
            self.assertEqual(script.main(), 0)

        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["actor"], "sync")
        db.commit.assert_called_once_with()
        db.rollback.assert_not_called()
        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
