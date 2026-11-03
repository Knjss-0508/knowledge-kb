"""迁移 revision id 的静态约束。

背景（2026-10-10 线上部署踩坑）：``alembic_version.version_num`` 是
``varchar(32)``（alembic 默认宽度，线上实测也是 32）。revision id
``20261010_01_conversation_identity``（33 字符）在 PostgreSQL 上写版本号时抛
``psycopg2.errors.StringDataRightTruncation``，``alembic upgrade head`` 整体
回滚（DDL 未生效，版本号也没变）。所以这里用静态检查把「id 过长」挡在提交前。
"""

import ast
import re
import unittest
from pathlib import Path


VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MAX_REVISION_ID_LENGTH = 32


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _read_assignment(path: Path, name: str):
    """读取迁移文件顶层 ``name = "..."`` 的字面量值（不执行模块）。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return _literal(node.value)
    return None


def _load_revisions() -> dict[str, dict]:
    revisions: dict[str, dict] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        revision = _read_assignment(path, "revision")
        if revision is None:
            continue
        revisions[revision] = {
            "path": path,
            "down_revision": _read_assignment(path, "down_revision"),
        }
    return revisions


class MigrationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.revisions = _load_revisions()

    def test_revision_ids_fit_the_alembic_version_column(self):
        self.assertGreater(len(self.revisions), 0)
        too_long = {
            revision: info["path"].name
            for revision, info in self.revisions.items()
            if len(revision) > MAX_REVISION_ID_LENGTH
        }
        self.assertEqual(
            too_long,
            {},
            "revision id 超过 {} 个字符会被 varchar(32) 的 "
            "alembic_version.version_num 截断".format(MAX_REVISION_ID_LENGTH),
        )

    def test_revision_ids_are_unique(self):
        names = [
            path.name
            for path in sorted(VERSIONS_DIR.glob("*.py"))
            if not path.name.startswith("__")
            and _read_assignment(path, "revision") is not None
        ]
        self.assertEqual(len(names), len(self.revisions))

    def test_history_has_a_single_head(self):
        referenced = {
            parent
            for info in self.revisions.values()
            for parent in (
                info["down_revision"]
                if isinstance(info["down_revision"], (tuple, list))
                else [info["down_revision"]]
            )
            if parent
        }
        self.assertFalse(referenced - self.revisions.keys(), "迁移依赖不存在")
        heads = sorted(set(self.revisions) - referenced)
        self.assertEqual(len(heads), 1, "迁移链出现分叉：{}".format(heads))

    def test_work_order_identity_follows_question_form_id(self):
        revision = "20261010_01_work_order_identity"
        self.assertIn(revision, self.revisions)
        self.assertEqual(
            self.revisions[revision]["down_revision"],
            "20261008_01_question_form_id",
        )
        self.assertEqual(
            self.revisions[revision]["path"].name,
            "20261010_01_work_order_identity.py",
        )

    def test_candidate_pause_follows_deployed_jsonb_migration(self):
        self.assertEqual(
            self.revisions["20261028_01_candidate_pause"]["down_revision"],
            "20261029_01_candidate_jsonb",
        )

    def test_dropped_retrieval_indexes_are_not_required_by_schema_validation(self):
        """20261103_01_drop_idx 删除的索引不能还留在强校验清单里。

        迁移会把这些索引 DROP，但 initialize_database.validate_schema 仍要求
        它们存在时，migrate 容器会在升级成功后报「索引缺失」并以非 0 退出，
        整个后端起不来。
        """

        source = (
            VERSIONS_DIR.parents[1] / "app" / "scripts" / "initialize_database.py"
        ).read_text(encoding="utf-8")
        migration = self.revisions["20261103_01_drop_idx"]["path"].read_text(
            encoding="utf-8"
        )
        dropped = set(
            re.findall(r'"?(ix_retrieval_quality_events_[a-z_]+)"?', migration)
        )
        self.assertTrue(dropped)
        still_required = sorted(
            name for name in dropped if f'"{name}"' in source
        )
        self.assertEqual(
            still_required,
            [],
            "这些索引已被迁移删除，但仍被 validate_schema 强制要求存在",
        )
