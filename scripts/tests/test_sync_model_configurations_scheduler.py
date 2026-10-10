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
    LarkCli,
    SourceMapping,
    CELLS_GET_CONTRACT_GUIDANCE,
    build_payload,
    discover_wiki_sources,
    run_once,
    _find_target_sheet,
    _load_source_mappings,
    _mapping_matches,
    _resolve_source_mapping,
    _source_state_dir,
    _spreadsheet_fingerprint,
    _sync_backend,
    _token_fingerprint,
)


def _workbook():
    return {
        "ok": True,
        "data": {
            "sheets": [
                {"sheet_id": "w3Caff", "row_count": 2, "column_count": 6},
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

    def cells(self, *, row_count, column_count):
        self.last_row_count = row_count
        self.last_column_count = column_count
        return self.cells_response


class FakeWikiLark:
    def __init__(self, *, workbooks, cells_by_token, revisions_by_token):
        self.workbooks = workbooks
        self.cells_by_token = cells_by_token
        self.revisions_by_token = {
            token: list(revisions)
            for token, revisions in revisions_by_token.items()
        }
        self.children = {
            "root": [
                {
                    "node_token": "phone-node",
                    "space_id": "space-1",
                    "has_child": False,
                    "obj_type": "sheet",
                    "obj_token": "phone-book",
                    "title": "手机配置",
                },
                {
                    "node_token": "folder-node",
                    "space_id": "space-1",
                    "has_child": True,
                    "obj_type": "docx",
                    "obj_token": "folder-doc",
                    "title": "分类目录",
                },
            ],
            "folder-node": [
                {
                    "node_token": "tablet-node",
                    "space_id": "space-1",
                    "has_child": False,
                    "obj_type": "sheet",
                    "obj_token": "tablet-book",
                    "title": "平板配置",
                }
            ],
        }

    def wiki_node(self, _node_token):
        return {
            "node_token": "root",
            "space_id": "space-1",
            "has_child": True,
            "obj_type": "docx",
            "obj_token": "root-doc",
            "title": "机型配置知识库",
        }

    def wiki_children(self, *, space_id, parent_node_token, max_pages):
        assert space_id == "space-1"
        assert max_pages >= 1
        return self.children.get(parent_node_token, [])

    def workbook_info(self, *, spreadsheet_token):
        return self.workbooks[spreadsheet_token]

    def revision(self, *, spreadsheet_token):
        values = self.revisions_by_token[spreadsheet_token]
        if not values:
            raise AssertionError(f"{spreadsheet_token} revision 调用次数超出测试预期")
        return values.pop(0)

    def cells(self, *, row_count, column_count, spreadsheet_token, sheet_id):
        assert row_count == 2
        assert column_count in {6, 26}
        assert sheet_id in {"phone-sheet", "tablet-sheet"}
        return self.cells_by_token[spreadsheet_token]


def _wiki_workbook(*, title, sheet_id):
    return {
        "ok": True,
        "data": {
            "title": title,
            "sheets": [
                {
                    "sheet_id": sheet_id,
                    "title": "个性化配置信息",
                    "row_count": 2,
                    "column_count": 6,
                }
            ],
        },
    }


def _wiki_source_config(directory):
    path = Path(directory) / "sources.json"
    path.write_text(
        json.dumps(
            {
                "include_default_mappings": False,
                "sources": [
                    {
                        "spreadsheet_token": "phone-book",
                        "category_id": "101",
                        "category_name": "手机",
                    },
                    {
                        "spreadsheet_token": "tablet-book",
                        "category_id": "119",
                        "category_name": "平板电脑",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


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
        workbook = _workbook()
        workbook["data"]["sheets"][0]["row_count"] = 3
        cells = _cells()
        cells["data"]["ranges"][0]["actual_range"] = "A1:F3"
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
                workbook_response=workbook,
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_uses_comprehensive_information_alias_and_supports_column_z(self):
        workbook = _workbook()
        workbook["data"]["sheets"][0]["column_count"] = 26
        cells = _cells()
        sheet_range = cells["data"]["ranges"][0]
        sheet_range["actual_range"] = "A1:Z2"
        sheet_range["col_indices"] = [
            chr(ord("A") + index) for index in range(26)
        ]
        sheet_range["cells"][0][5]["value"] = "综合信息"
        sheet_range["cells"][1][5]["value"] = "镜头综合信息"
        for row in sheet_range["cells"]:
            row.extend({"value": ""} for _ in range(20))

        payload = build_payload(
            workbook_response=workbook,
            cells_response=cells,
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["records"][0]["content"], "镜头综合信息")
        self.assertEqual(payload["records"][0]["source_fields"]["综合信息"], "镜头综合信息")

    def test_prefers_comprehensive_content_when_both_content_headers_exist(self):
        workbook = _workbook()
        workbook["data"]["sheets"][0]["column_count"] = 7
        cells = _cells()
        sheet_range = cells["data"]["ranges"][0]
        sheet_range["actual_range"] = "A1:G2"
        sheet_range["col_indices"].append("G")
        sheet_range["cells"][0].append({"value": "综合信息"})
        sheet_range["cells"][1].append({"value": "不应选取"})

        payload = build_payload(
            workbook_response=workbook,
            cells_response=cells,
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["records"][0]["content"], "综合内容 A")

    def test_source_record_id_accepts_the_current_template_alias(self):
        workbook = _workbook()
        workbook["data"]["sheets"][0]["column_count"] = 7
        payload = build_payload(
            workbook_response=workbook,
            cells_response=_cells_with_source_alias("来源知识ID"),
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["records"][0]["source_record_id"], "external-001")

    def test_invisible_format_characters_are_removed_from_identity_fields(self):
        cells = _cells()
        cells["data"]["ranges"][0]["cells"][1][2]["value"] = "LDBO\u200c\u200c"

        payload = build_payload(
            workbook_response=_workbook(),
            cells_response=cells,
            sheet_id="w3Caff",
            spreadsheet_token="sheet-token",
        )

        self.assertEqual(payload["records"][0]["brand_name"], "LDBO")
        self.assertEqual(payload["records"][0]["source_fields"]["品牌"], "LDBO")

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

    def test_cells_range_must_cover_metadata_row_count(self):
        cells = _cells()
        cells["data"]["ranges"][0]["actual_range"] = "A1:F1"

        with self.assertRaisesRegex(SyncSchedulerError, "未覆盖目标工作表全部行"):
            build_payload(
                workbook_response=_workbook(),
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_row_indices_must_be_exactly_all_physical_rows(self):
        workbook = _workbook()
        workbook["data"]["sheets"][0]["row_count"] = 3
        cells = _cells()
        cells["data"]["ranges"][0]["actual_range"] = "A1:F3"
        cells["data"]["ranges"][0]["row_indices"] = [1, 3]

        with self.assertRaisesRegex(SyncSchedulerError, "严格覆盖 1..目标行数"):
            build_payload(
                workbook_response=workbook,
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_col_indices_must_be_contiguous_to_actual_range(self):
        workbook = _workbook()
        workbook["data"]["sheets"][0]["column_count"] = 7
        cells = _cells()
        cells["data"]["ranges"][0]["actual_range"] = "A1:G2"
        cells["data"]["ranges"][0]["col_indices"] = ["A", "B", "D", "E", "F", "G"]

        with self.assertRaisesRegex(SyncSchedulerError, "从 A 连续覆盖"):
            build_payload(
                workbook_response=workbook,
                cells_response=cells,
                sheet_id="w3Caff",
                spreadsheet_token="sheet-token",
            )

    def test_sheet_id_is_strict_and_name_does_not_fallback(self):
        workbook = {
            "data": {
                "sheets": [
                    {"sheet_id": "other", "title": "个性化配置信息", "row_count": 2}
                ]
            }
        }
        with self.assertRaisesRegex(SyncSchedulerError, "未找到目标工作表"):
            _find_target_sheet(
                workbook,
                "expected",
                sheet_name="个性化配置信息",
            )


class ScheduledSyncTests(unittest.TestCase):
    def test_cells_reads_dynamic_range_through_column_z(self):
        with tempfile.TemporaryDirectory() as directory:
            lark = LarkCli(_config(directory))
            with patch.object(lark, "call", return_value={"ok": True}) as call:
                lark.cells(row_count=2, column_count=26)

        arguments = call.call_args.args[0]
        self.assertIn("A1:Z2", arguments)

    def test_wiki_children_accepts_lark_cli_nodes_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            lark = LarkCli(_config(directory))
            with patch.object(
                lark,
                "call",
                return_value={
                    "ok": True,
                    "data": {
                        "nodes": [{"node_token": "child", "obj_type": "sheet"}],
                        "has_more": False,
                    },
                },
            ):
                children = lark.wiki_children(
                    space_id="123",
                    parent_node_token="parent",
                    max_pages=1,
                )

        self.assertEqual(children, [{"node_token": "child", "obj_type": "sheet"}])

    def test_workbook_warning_redacts_the_current_source_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory, spreadsheet_token="default-source-token")
            lark = LarkCli(config)
            with patch.object(
                lark,
                "call",
                return_value={
                    "ok": True,
                    "data": {"warning_message": "warning current-source-token"},
                },
            ):
                with self.assertRaisesRegex(SyncSchedulerError, r"\[spreadsheet-redacted\]") as context:
                    lark.workbook_info(spreadsheet_token="current-source-token")

        self.assertNotIn("current-source-token", str(context.exception))

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
            self.assertEqual(
                command[-1],
                "docker exec -i kb-backend python -m "
                "app.scripts.sync_model_configurations - --actor "
                + config.actor,
            )
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


class MultiSourceSyncTests(unittest.TestCase):
    def _wiki_lark(self):
        return FakeWikiLark(
            workbooks={
                "phone-book": _wiki_workbook(
                    title="手机工作簿", sheet_id="phone-sheet"
                ),
                "tablet-book": _wiki_workbook(
                    title="平板工作簿", sheet_id="tablet-sheet"
                ),
            },
            cells_by_token={
                "phone-book": _cells(revision="1"),
                "tablet-book": _cells(revision="2"),
            },
            revisions_by_token={
                "phone-book": ["1", "1"],
                "tablet-book": ["2", "2"],
            },
        )

    def test_wiki_discovery_recurses_and_binds_category_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            sources, skipped, ignored = discover_wiki_sources(
                config,
                lark=self._wiki_lark(),
            )

        self.assertEqual(skipped, [])
        self.assertEqual(ignored, [])
        self.assertEqual(
            [(source.spreadsheet_token, source.category_id) for source in sources],
            [("phone-book", "101"), ("tablet-book", "119")],
        )
        self.assertEqual(sources[1].sheet_name, "个性化配置信息")

    def test_each_source_has_independent_checkpoint_and_payload_category(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            calls = []
            result = run_once(
                config,
                lark=self._wiki_lark(),
                backend_sync=lambda _config, payload: calls.append(payload)
                or {"status": "success", "created": 1},
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(len(result["sources"]), 2)
            self.assertEqual(
                {payload["category_id"] for payload in calls},
                {"101", "119"},
            )
            for source_token, category_id in (
                ("phone-book", "101"),
                ("tablet-book", "119"),
            ):
                source = next(
                    item
                    for item in result["sources"]
                    if item["category_id"] == category_id
                )
                state_path = Path(directory) / "sources"
                source_key = next(
                    source_obj.state_key
                    for source_obj in discover_wiki_sources(
                        config,
                        lark=self._wiki_lark(),
                    )[0]
                    if source_obj.spreadsheet_token == source_token
                )
                checkpoint = (
                    state_path / source_key / "state.json"
                )
                self.assertTrue(checkpoint.exists())

    def test_one_source_failure_does_not_block_other_source(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            calls = []

            def backend(_config, payload):
                calls.append(payload["category_id"])
                if payload["category_id"] == "101":
                    raise SyncSchedulerError("手机来源写库失败")
                return {"status": "success", "created": 1}

            result = run_once(
                config,
                lark=self._wiki_lark(),
                backend_sync=backend,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(calls, ["101", "119"])
            self.assertEqual(len(result["failed_sources"]), 1)
            self.assertEqual(result["failed_sources"][0]["sheet_id"], "phone-sheet")
            tablet_source = next(
                source
                for source in discover_wiki_sources(
                    config,
                    lark=self._wiki_lark(),
                )[0]
                if source.category_id == "119"
            )
            self.assertTrue(
                (_source_state_dir(config, tablet_source) / "state.json").exists()
            )
            phone_source = next(
                source
                for source in discover_wiki_sources(
                    config,
                    lark=self._wiki_lark(),
                )[0]
                if source.category_id == "101"
            )
            phone_state = json.loads(
                (_source_state_dir(config, phone_source) / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("写库失败", phone_state["last_error"]["message"])
            self.assertNotIn("wiki_node_token", phone_state)
            self.assertEqual(
                phone_state["wiki_node_token_sha256"],
                _token_fingerprint("phone-node"),
            )
            self.assertNotIn("phone-node", json.dumps(phone_state, ensure_ascii=False))

    def test_unexpected_source_exception_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )

            def backend(_config, payload):
                if payload["category_id"] == "101":
                    raise ValueError("unexpected backend exception")
                return {"status": "success", "created": 1}

            result = run_once(
                config,
                lark=self._wiki_lark(),
                backend_sync=backend,
            )

            self.assertEqual(result["status"], "partial")
            self.assertIn("unexpected backend exception", result["failed_sources"][0]["reason"])
            self.assertEqual(len(result["sources"]), 1)

    def test_missing_target_sheet_is_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            lark = self._wiki_lark()
            lark.workbooks["phone-book"]["data"]["sheets"][0]["title"] = "已改名"
            sources, skipped, ignored = discover_wiki_sources(config, lark=lark)

        self.assertEqual(len(sources), 1)
        self.assertTrue(
            any(item["reason"] == "target_sheet_missing" for item in skipped)
        )
        serialized = json.dumps(skipped, ensure_ascii=False)
        self.assertIn("node_fingerprint", serialized)
        self.assertNotIn('"node_token"', serialized)
        self.assertNotIn("phone-node", serialized)
        self.assertEqual(ignored, [])

    def test_unmapped_non_target_workbook_is_ignored_without_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            lark = self._wiki_lark()
            lark.children["root"].append(
                {
                    "node_token": "game-node",
                    "space_id": "space-1",
                    "has_child": False,
                    "obj_type": "sheet",
                    "obj_token": "game-book",
                    "title": "游戏机",
                }
            )
            lark.workbooks["game-book"] = {
                "ok": True,
                "data": {
                    "title": "游戏机",
                    "sheets": [
                        {
                            "sheet_id": "game-sheet",
                            "title": "普通数据",
                            "row_count": 2,
                            "column_count": 6,
                        }
                    ],
                },
            }
            sources, skipped, ignored = discover_wiki_sources(config, lark=lark)
            self.assertEqual(len(sources), 2)
            self.assertEqual(skipped, [])
            self.assertEqual(ignored[0]["workbook_title"], "游戏机")
            serialized = json.dumps(ignored, ensure_ascii=False)
            self.assertIn("node_fingerprint", serialized)
            self.assertNotIn('"node_token"', serialized)
            self.assertNotIn("game-node", serialized)

            result = run_once(
                config,
                lark=lark,
                backend_sync=lambda *_: {"status": "success", "created": 1},
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            result["ignored_non_target_sources"][0]["workbook_title"], "游戏机"
        )

    def test_known_actual_workbook_alias_resolves_to_existing_category(self):
        with tempfile.TemporaryDirectory() as directory:
            mappings, _ = _load_source_mappings(_config(directory))
            mapping = _resolve_source_mapping(
                mappings,
                spreadsheet_token="watch-book",
                wiki_node_token="watch-node",
                workbook_title="智能手表",
                sheet_name="个性化配置信息",
                sheet_id="2TrbJP",
            )

        self.assertIsNotNone(mapping)
        self.assertEqual((mapping.category_id, mapping.category_name), ("1100000170", "手表"))

    def test_custom_mapping_replaces_default_by_same_sheet_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "sheet_id": "w3Caff",
                                "category_id": "999",
                                "category_name": "测试类目",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            mappings, _ = _load_source_mappings(
                _config(directory, source_config_path=path)
            )

        matched = [mapping for mapping in mappings if mapping.sheet_id == "w3Caff"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].category_id, "999")

    def test_example_source_config_uses_strong_sheet_ids_without_title_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = SCRIPT_ROOT.parent / "deploy" / "systemd" / "model-configuration-sources.example.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(document["include_default_mappings"])
            self.assertEqual(len(document["sources"]), 10)
            self.assertTrue(all(item.get("sheet_id") for item in document["sources"]))
            self.assertTrue(
                all("workbook_title" not in item for item in document["sources"])
            )
            mappings, _ = _load_source_mappings(
                _config(directory, source_config_path=path)
            )
            watch = next(item for item in document["sources"] if item["sheet_id"] == "2TrbJP")
            mapping = _resolve_source_mapping(
                mappings,
                spreadsheet_token=watch["spreadsheet_token"],
                wiki_node_token=watch["wiki_node_token"],
                workbook_title="智能手表",
                sheet_name="个性化配置信息",
                sheet_id="2TrbJP",
            )

        self.assertIsNotNone(mapping)
        self.assertEqual((mapping.category_id, mapping.category_name), ("1100000170", "手表"))

    def test_strong_spreadsheet_token_cannot_fallback_to_shared_sheet_id(self):
        mapping = SourceMapping(
            category_id="101",
            category_name="手机",
            spreadsheet_token="expected-book",
            sheet_id="shared-sheet",
            workbook_title="手机工作簿",
        )

        self.assertFalse(
            _mapping_matches(
                mapping,
                spreadsheet_token="other-book",
                wiki_node_token="other-node",
                workbook_title="手机工作簿",
                sheet_name="个性化配置信息",
                sheet_id="shared-sheet",
            )
        )

    def test_custom_mapping_takes_priority_over_default_title_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override-token.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "spreadsheet_token": "new-phone-token",
                                "category_id": "999",
                                "category_name": "测试类目",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            mappings, _ = _load_source_mappings(
                _config(directory, source_config_path=path)
            )
            mapping = _resolve_source_mapping(
                mappings,
                spreadsheet_token="new-phone-token",
                wiki_node_token="node",
                workbook_title="大模型【手机】知识库",
                sheet_name="个性化配置信息",
                sheet_id="other-sheet",
            )

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.category_id, "999")

    def test_mapping_cannot_match_every_workbook_by_sheet_name_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(
                json.dumps(
                    {
                        "include_default_mappings": False,
                        "sources": [
                            {
                                "sheet_name": "个性化配置信息",
                                "category_id": "999",
                                "category_name": "测试类目",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SyncSchedulerError, "不能仅按 sheet_name"):
                _load_source_mappings(_config(directory, source_config_path=path))

    def test_sheet_name_is_an_extra_constraint_not_a_locator(self):
        mapping = SourceMapping(
            category_id="101",
            category_name="手机",
            workbook_title="手机工作簿",
            sheet_name="个性化配置信息",
        )
        self.assertTrue(
            _mapping_matches(
                mapping,
                spreadsheet_token="phone-book",
                wiki_node_token="phone-node",
                workbook_title="手机工作簿",
                sheet_name="个性化配置信息",
            )
        )
        self.assertFalse(
            _mapping_matches(
                mapping,
                spreadsheet_token="other-book",
                wiki_node_token="other-node",
                workbook_title="其他工作簿",
                sheet_name="个性化配置信息",
            )
        )

    def test_corrupt_source_checkpoint_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            phone_source = next(
                source
                for source in discover_wiki_sources(
                    config,
                    lark=self._wiki_lark(),
                )[0]
                if source.category_id == "101"
            )
            checkpoint = _source_state_dir(config, phone_source) / "state.json"
            checkpoint.parent.mkdir(parents=True)
            corrupt_content = "{not-valid-json"
            checkpoint.write_text(corrupt_content, encoding="utf-8")

            result = run_once(
                config,
                lark=self._wiki_lark(),
                backend_sync=lambda *_: {"status": "success", "created": 1},
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(checkpoint.read_text(encoding="utf-8"), corrupt_content)
            failure = next(
                item
                for item in result["failed_sources"]
                if item["sheet_id"] == "phone-sheet"
            )
            self.assertIn("checkpoint 损坏", failure["reason"])

    def test_missing_previous_source_marks_wiki_run_partial_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                directory,
                wiki_root="wiki-root",
                source_config_path=_wiki_source_config(directory),
            )
            first = run_once(
                config,
                lark=self._wiki_lark(),
                backend_sync=lambda *_: {"status": "success", "created": 1},
            )
            self.assertEqual(first["status"], "success")

            second_lark = self._wiki_lark()
            second_lark.children["folder-node"] = []
            second = run_once(
                config,
                lark=second_lark,
                backend_sync=lambda *_: {"status": "success", "created": 1},
            )

            self.assertEqual(second["status"], "partial")
            self.assertEqual(len(second["missing_previous_sources"]), 1)
            missing = second["missing_previous_sources"][0]
            self.assertEqual(missing["sheet_id"], "tablet-sheet")
            self.assertEqual(missing["reason"], "missing_previous_source")
            self.assertTrue(
                (Path(directory) / "sources" / missing["source_state_key"] / "state.json").exists()
            )
            serialized = json.dumps(second, ensure_ascii=False)
            self.assertNotIn('"node_token"', serialized)
            self.assertNotIn("tablet-node", serialized)


if __name__ == "__main__":
    unittest.main()


