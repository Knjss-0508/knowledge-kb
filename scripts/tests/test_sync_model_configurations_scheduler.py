import json
import sys
import tempfile
import unittest
from subprocess import CompletedProcess
from pathlib import Path
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from sync_model_configurations_scheduler import (  # noqa: E402
    SchedulerConfig,
    SyncSchedulerError,
    CELLS_GET_CONTRACT_GUIDANCE,
    build_payload,
    run_once,
    _spreadsheet_fingerprint,
    _sync_backend,
)


def _workbook():
    return {
        "ok": True,
        "data": {
            "sheets": [
                {"sheet_id": "w3Caff", "row_count": 2},
            ]
        },
    }


def _cells(*, content="综合内容 A", model_id="97519", revision="1"):
    return {
        "ok": True,
        "data": {
            "revision": revision,
            "has_more": False,
            "ranges": [
                {
                    "truncated": False,
                    "actual_range": "A1:F2",
                    "col_indices": ["A", "B", "C", "D", "E", "F"],
                    "row_indices": [1, 2],
                    "cells": [
                        [
                            {"value": "标题"},
                            {"value": "品牌ID"},
                            {"value": "品牌"},
                            {"value": "型号ID"},
                            {"value": "型号"},
                            {"value": "综合内容"},
                        ],
                        [
                            {"value": "iPad 配置"},
                            {"value": "10530"},
                            {"value": "苹果"},
                            {"value": model_id},
                            {"value": "iPad 10"},
                            {"value": content},
                        ],
                    ],
                }
            ],
        },
    }


def _cells_with_source_alias(header, value="external-001"):
    cells = _cells()
    header_row = cells["data"]["ranges"][0]["cells"][0]
    header_row.append({"value": header})
    cells["data"]["ranges"][0]["cells"][1].append({"value": value})
    cells["data"]["ranges"][0]["col_indices"].append("G")
    cells["data"]["ranges"][0]["actual_range"] = "A1:G2"
    return cells


class FakeLark:
    def __init__(self, revisions, cells_response):
        self.revisions = list(revisions)
        self.cells_response = cells_response

    def revision(self):
        if not self.revisions:
            raise AssertionError("revision 调用次数超出测试预期")
        return self.revisions.pop(0)

    def workbook_info(self):
        return _workbook()

    def cells(self, *, row_count):
        self.last_row_count = row_count
        return self.cells_response


def _config(state_dir, **overrides):
    values = dict(
        identity="bot",
        spreadsheet_token="sheet-token",
        sheet_id="w3Caff",
        state_dir=Path(state_dir),
        lark_cli="lark-cli",
        docker_cli="docker",
        backend_container="kb-backend",
        target="local",
        ssh_cli="ssh",
        ssh_host="",
        ssh_user="root",
        ssh_key=None,
        actor="model-configuration-sync",
        max_revision_retries=3,
        command_timeout_seconds=900,
        force=False,
        check_only=False,
    )
    values.update(overrides)
    return SchedulerConfig(**values)


class BuildPayloadTests(unittest.TestCase):
    def test_required_fields_and_model_key_are_validated_before_sync(self):
        payload = build_payload(
            workbook_response=_workbook(),
            cells_response=_cells(),
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["revision"], "1")
        self.assertEqual(payload["category_id"], "119")
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["model_id"], "97519")
        self.assertEqual(payload["records"][0]["source_record_id"], "")

    def test_missing_required_header_is_rejected(self):
        cells = _cells()
        cells["data"]["ranges"][0]["cells"][0].pop()

        with self.assertRaisesRegex(SyncSchedulerError, "缺少必填列"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_duplicate_model_key_is_rejected(self):
        cells = _cells()
        cells["data"]["ranges"][0]["cells"].append(
            [
                {"value": "另一个标题"},
                {"value": "10530"},
                {"value": "苹果"},
                {"value": "97519"},
                {"value": "另一个机型"},
                {"value": "另一个内容"},
            ]
        )
        cells["data"]["ranges"][0]["row_indices"].append(3)

        with self.assertRaisesRegex(SyncSchedulerError, "组合重复"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_source_record_id_accepts_the_current_template_alias(self):
        payload = build_payload(
            workbook_response=_workbook(),
            cells_response=_cells_with_source_alias("来源知识ID"),
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["records"][0]["source_record_id"], "external-001")

    def test_read_warning_is_fail_closed(self):
        cells = _cells()
        cells["data"]["warning_message"] = "结果达到输出上限"

        with self.assertRaisesRegex(SyncSchedulerError, "告警"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_contract_guidance_warning_is_allowed_after_metadata_checks(self):
        cells = _cells()
        cells["data"]["warning_message"] = (
            "处理 ranges[n].cells 之前，必须先查看顶层 has_more，以及每个 range 的 "
            "actual_range / row_indices / col_indices。定位真实行号时用 row_indices[i]，"
            "定位真实列字母时用 col_indices[j]，不要按二维数组下标自己数行列；"
            "skip_hidden=true、skip_filter=true 或结果被截断时，这样会错位。"
        )

        payload = build_payload(
            workbook_response=_workbook(),
            cells_response=cells,
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(len(payload["records"]), 1)

    def test_contract_guidance_with_extra_warning_is_rejected(self):
        cells = _cells()
        cells["data"]["warning_message"] = (
            CELLS_GET_CONTRACT_GUIDANCE + " 另有结果达到输出上限。"
        )

        with self.assertRaisesRegex(SyncSchedulerError, "告警"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_contract_guidance_does_not_override_has_more(self):
        cells = _cells()
        cells["data"]["warning_message"] = CELLS_GET_CONTRACT_GUIDANCE
        cells["data"]["has_more"] = True

        with self.assertRaisesRegex(SyncSchedulerError, "读取不完整"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_contract_guidance_does_not_override_range_truncation(self):
        cells = _cells()
        cells["data"]["warning_message"] = CELLS_GET_CONTRACT_GUIDANCE
        cells["data"]["ranges"][0]["truncated"] = True

        with self.assertRaisesRegex(SyncSchedulerError, "被截断"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )


class ScheduledSyncTests(unittest.TestCase):
    def test_ssh_target_sends_json_to_the_backend_container(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "id_ed25519"
            key.write_text("placeholder", encoding="utf-8")
            config = _config(
                directory,
                target="ssh",
                ssh_host="81.71.6.245",
                ssh_key=key,
            )
            payload = {"records": [{"model_id": "97519"}]}
            with patch(
                "sync_model_configurations_scheduler.subprocess.run",
                return_value=CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=b'{"status":"success","created":1}',
                    stderr=b"",
                ),
            ) as runner:
                result = _sync_backend(config, payload)

            self.assertEqual(result["status"], "success")
            command = runner.call_args.args[0]
            self.assertEqual(command[0], "ssh")
            self.assertIn("root@81.71.6.245", command)
            self.assertIn("exec -i kb-backend", command[-1])
            self.assertEqual(
                runner.call_args.kwargs["input"],
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

    def test_revision_unchanged_skips_without_reading_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory)
            state = {
                "schema_version": 1,
                "spreadsheet_token_sha256": _spreadsheet_fingerprint("sheet-token"),
                "sheet_id": "w3Caff",
                "last_success_revision": "1",
                "last_payload_sha256": "old",
            }
            config.state_path.write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            lark = FakeLark(["1"], _cells())
            calls = []

            result = run_once(
                config,
                lark=lark,
                backend_sync=lambda *_: calls.append(True),
            )

            self.assertEqual(result["reason"], "revision_unchanged")
            self.assertEqual(calls, [])

    def test_revision_change_syncs_then_persists_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory)
            lark = FakeLark(["2", "2"], _cells(revision="2"))
            calls = []

            result = run_once(
                config,
                lark=lark,
                backend_sync=lambda _config, payload: calls.append(payload)
                or {"status": "success", "created": 1},
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(len(calls), 1)
            checkpoint = json.loads(config.state_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["last_success_revision"], "2")
            self.assertEqual(checkpoint["last_result"]["created"], 1)

    def test_revision_changes_during_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory, max_revision_retries=1)
            lark = FakeLark(["2", "3"], _cells(revision="2"))

            with self.assertRaisesRegex(SyncSchedulerError, "持续变化"):
                run_once(config, lark=lark, backend_sync=lambda *_: None)

            self.assertFalse(config.state_path.exists())

    def test_same_content_on_new_revision_skips_backend_write(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory)
            first_lark = FakeLark(["1", "1"], _cells(revision="1"))
            run_once(
                config,
                lark=first_lark,
                backend_sync=lambda *_: {"status": "success"},
            )

            second_lark = FakeLark(["2", "2"], _cells(revision="2"))
            calls = []
            result = run_once(
                config,
                lark=second_lark,
                backend_sync=lambda *_: calls.append(True),
            )

            self.assertEqual(result["reason"], "content_unchanged")
            self.assertEqual(calls, [])
            checkpoint = json.loads(config.state_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["last_success_revision"], "2")

    def test_checkpoint_for_another_sheet_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory)
            config.state_path.write_text(
                json.dumps({"sheet_id": "another-sheet"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SyncSchedulerError, "工作表"):
                run_once(
                    config,
                    lark=FakeLark(["1"], _cells()),
                    backend_sync=lambda *_: None,
                )

    def test_legacy_checkpoint_without_fingerprint_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory)
            config.state_path.write_text(
                json.dumps({"last_success_revision": "1"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SyncSchedulerError, "缺少工作簿指纹"):
                run_once(
                    config,
                    lark=FakeLark(["1"], _cells()),
                    backend_sync=lambda *_: None,
                )


if __name__ == "__main__":
    unittest.main()

