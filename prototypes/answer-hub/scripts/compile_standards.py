from __future__ import annotations

from pathlib import Path
import argparse
import json

from answer_hub.standards_compiler import (
    compile_standard_catalog,
    discover_default_standard_sources,
    load_standard_source_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="编译配置品类的原始质检标准为轻量标准目录")
    parser.add_argument(
        "--downloads-dir",
        default=str(Path.home() / "Downloads"),
        help="兼容旧四品类默认标准文件所在目录",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="标准源 JSON 清单；配置新品类时优先使用",
    )
    parser.add_argument(
        "--existing-knowledge",
        default="",
        help="可选：已有知识库 Excel，用于优先匹配",
    )
    parser.add_argument(
        "--output",
        default="data/compiled_standards/active_standards.json",
        help="输出 JSON 路径",
    )
    args = parser.parse_args()
    if args.manifest:
        sources, active_sheets = load_standard_source_manifest(Path(args.manifest))
    else:
        sources = discover_default_standard_sources(args.downloads_dir)
        active_sheets = None
    summary = compile_standard_catalog(
        sources,
        args.output,
        active_sheets=active_sheets,
        existing_knowledge_path=args.existing_knowledge or None,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
