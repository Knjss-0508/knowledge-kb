from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from answer_hub.cli import main
from answer_hub.topic_label_model import (
    build_topic_feature_text,
    load_human_review_samples,
    load_topic_label_model,
    train_topic_label_model,
)


def _pseudo_theme(
    theme_id: str,
    *,
    issue: str,
    topic_stage: str,
    knowledge_value: str,
) -> dict[str, object]:
    return {
        "theme_id": theme_id,
        "member_count": 2,
        "product_categories": ["手机"],
        "normalized_issues": [issue],
        "intents": [issue],
        "subjects": ["测试对象"],
        "phenomena": [issue],
        "judgment_targets": [issue],
        "resolution_modes": [issue],
        "thresholds_or_exceptions": [issue],
        "evidence_summaries": [issue],
        "classification_status": "ok",
        "prediction": {
            "topic_stage": topic_stage,
            "knowledge_value": knowledge_value,
            "confidence": 0.9,
            "needs_human_review": False,
        },
    }


def _write_balanced_pseudo_labels(path: Path) -> None:
    category_examples = {
        "质检标准": [
            "屏幕划痕算不算合格，需要明确判定阈值和边界",
            "漏液面积达到多少应选择显示问题，属于判定口径",
            "外壳磕碰是否降级，需要明确适用条件和例外",
        ],
        "质检流程": [
            "怎么进入设置读取电池循环次数，给出操作步骤",
            "如何查询设备序列号，先打开页面再核对信息",
            "怎样检测摄像头，依次拍照并检查成像结果",
        ],
        "案例解析": [
            "请看当前图片，这台机器的屏幕现象是否异常",
            "只依据本次视频判断这个转轴声音是否正常",
            "分析当前案例照片中的镜头划痕，不能外推",
        ],
        "课外常识": [
            "这个型号是否支持无线充电，查询产品功能",
            "某版本配置多大内存，属于型号基础信息",
            "原装配件包含哪些内容，属于产品常识",
        ],
        "不确定": [
            "问题描述互相冲突，证据不足，需要人工确认",
            "对象和诉求不清楚，无法判断属于哪个环节",
            "会话缺少关键上下文，只能进入人工复核",
        ],
    }
    themes: list[dict[str, object]] = []
    index = 1
    for category, examples in category_examples.items():
        for position, issue in enumerate(examples):
            value = "值得沉淀" if position % 2 == 0 else "不值得沉淀"
            if value == "值得沉淀":
                issue += "，包含可复用规则、步骤或明确边界"
            else:
                issue += "，只有单个案例结论且缺少可复用规则"
            themes.append(
                _pseudo_theme(
                    f"TOP-{index:03d}",
                    issue=issue,
                    topic_stage=category,
                    knowledge_value=value,
                )
            )
            index += 1
    path.write_text(
        json.dumps({"metadata": {}, "themes": themes}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_feature_text_excludes_identifiers_teacher_output_and_sensitive_values() -> None:
    topic = {
        "theme_id": "TOP-SECRET",
        "source_sample_ids": ["SOURCE-SECRET"],
        "source_work_order_ids": ["2077208618029027906"],
        "category_l1": ["旧分类秘密"],
        "normalized_issues": ["屏幕划痕怎么判，联系电话13812345678"],
        "product_categories": ["手机"],
        "prediction": {
            "stage_reason": "教师答案秘密",
            "value_reason": "教师价值理由秘密",
        },
    }

    text = build_topic_feature_text(topic)

    assert "屏幕划痕怎么判" in text
    assert "SOURCE-SECRET" not in text
    assert "2077208618029027906" not in text
    assert "旧分类秘密" not in text
    assert "教师答案秘密" not in text
    assert "13812345678" not in text


def test_human_review_loader_uses_only_rows_selected_for_training(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "review.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "聚类结果"
    sheet.append(
        [
            "聚类主题ID",
            "聚类主题",
            "产品类型",
            "主题对象/部位",
            "主题异常现象",
            "主题解题方式",
            "成员核心问题",
            "人工主题问题分类",
            "人工是否值得沉淀",
            "是否进入训练集",
        ]
    )
    sheet.append(
        [
            "TOP-1",
            "屏幕坏点怎么判",
            "手机",
            "屏幕",
            "坏点",
            "判定边界",
            "坏点达到什么条件算异常",
            "质检标准",
            "值得沉淀",
            "是",
        ]
    )
    sheet.append(
        [
            "TOP-2",
            "怎么查电池循环",
            "手机",
            "电池",
            "循环次数",
            "操作步骤",
            "进入设置读取循环次数",
            "质检流程",
            "值得沉淀",
            "否",
        ]
    )
    workbook.save(workbook_path)

    samples = load_human_review_samples(workbook_path)

    assert len(samples) == 1
    assert samples[0].sample_id == "TOP-1"
    assert samples[0].topic_stage == "质检标准"
    assert samples[0].knowledge_value == "值得沉淀"


def test_pseudo_label_training_saves_loadable_two_head_model(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "topic_stage_predictions.json"
    output_dir = tmp_path / "model"
    _write_balanced_pseudo_labels(source_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    low_confidence_theme = _pseudo_theme(
        "TOP-LOW",
        issue="低置信度教师标签不应进入训练",
        topic_stage="质检标准",
        knowledge_value="值得沉淀",
    )
    low_confidence_theme["prediction"]["confidence"] = 0.4
    payload["themes"].append(low_confidence_theme)
    upstream_risk_theme = _pseudo_theme(
        "TOP-UPSTREAM-RISK",
        issue="上游证据冲突的伪标签默认不进入训练",
        topic_stage="质检标准",
        knowledge_value="值得沉淀",
    )
    upstream_risk_theme["upstream_requires_review"] = True
    payload["themes"].append(upstream_risk_theme)
    source_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = train_topic_label_model(
        source_path,
        output_dir,
        allow_pseudo_labels=True,
        hash_size=2048,
        seed=7,
    )

    assert summary["sample_count"] == 15
    assert summary["label_source"] == "mimo_pseudo_labels"
    assert summary["production_eligible"] is False
    assert (output_dir / "model.npz").exists()
    assert (output_dir / "metadata.json").exists()
    assert (output_dir / "training_report.json").exists()
    report = json.loads(
        (output_dir / "training_report.json").read_text(encoding="utf-8")
    )
    assert report["pseudo_label_filter"]["skipped_low_confidence"] == 1
    assert report["pseudo_label_filter"]["skipped_upstream_risk"] == 1

    model = load_topic_label_model(output_dir)
    prediction = model.predict(
        {
            "product_categories": ["手机"],
            "normalized_issues": [
                "如何进入设置读取电池循环次数，需要按步骤操作"
            ],
            "intents": ["查询方法"],
            "resolution_modes": ["操作步骤"],
        }
    )

    assert prediction["topic_stage"] == "质检流程"
    assert prediction["knowledge_value"] in {"值得沉淀", "不值得沉淀"}
    assert prediction["needs_human_review"] is True
    assert "伪标签" in "；".join(prediction["review_reasons"])
    assert "生产验收" in "；".join(prediction["review_reasons"])
    assert 0 <= prediction["topic_stage_confidence"] <= 1
    assert 0 <= prediction["knowledge_value_confidence"] <= 1


def test_cli_trains_experimental_pseudo_label_model(
    tmp_path: Path,
    capsys,
) -> None:
    source_path = tmp_path / "topic_stage_predictions.json"
    output_dir = tmp_path / "model"
    _write_balanced_pseudo_labels(source_path)

    exit_code = main(
        [
            "train-topic-label-model",
            "--source",
            str(source_path),
            "--output-dir",
            str(output_dir),
            "--allow-pseudo-labels",
            "--hash-size",
            "1024",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "trained_experimental_baseline"
    assert output["force_human_review"] is True


def test_upstream_risk_forces_review_even_for_validated_model(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "topic_stage_predictions.json"
    output_dir = tmp_path / "model"
    _write_balanced_pseudo_labels(source_path)
    train_topic_label_model(
        source_path,
        output_dir,
        allow_pseudo_labels=True,
        hash_size=1024,
    )
    metadata_path = output_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        {
            "label_source": "human_review",
            "force_human_review": False,
            "production_eligible": True,
        }
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )

    prediction = load_topic_label_model(output_dir).predict(
        {
            "product_categories": ["手机"],
            "normalized_issues": ["屏幕划痕怎么判"],
            "upstream_requires_review": True,
        }
    )

    assert prediction["needs_human_review"] is True
    assert "上游" in "；".join(prediction["review_reasons"])


def test_cli_returns_json_error_for_missing_training_source(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main(
        [
            "train-topic-label-model",
            "--source",
            str(tmp_path / "missing.json"),
            "--output-dir",
            str(tmp_path / "model"),
            "--allow-pseudo-labels",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "failed"
    assert "missing.json" in output["error"]


def test_cli_batch_predicts_without_copying_source_text(
    tmp_path: Path,
    capsys,
) -> None:
    source_path = tmp_path / "topic_stage_predictions.json"
    model_dir = tmp_path / "model"
    prediction_path = tmp_path / "predictions.json"
    _write_balanced_pseudo_labels(source_path)
    train_topic_label_model(
        source_path,
        model_dir,
        allow_pseudo_labels=True,
        hash_size=1024,
    )

    exit_code = main(
        [
            "predict-topic-label-model",
            "--model-dir",
            str(model_dir),
            "--source",
            str(source_path),
            "--output",
            str(prediction_path),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    payload = json.loads(prediction_path.read_text(encoding="utf-8"))
    serialized = prediction_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert summary["successful_count"] == 15
    assert summary["failed_count"] == 0
    assert len(payload["predictions"]) == 15
    assert set(payload["predictions"][0]) == {
        "theme_id",
        "status",
        "prediction",
    }
    assert "屏幕划痕算不算合格" not in serialized


def test_cli_returns_json_error_for_corrupt_model(
    tmp_path: Path,
    capsys,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "metadata.json").write_text(
        json.dumps({"model_version": "topic-label-hash-nb-v1"}),
        encoding="utf-8",
    )
    (model_dir / "model.npz").write_bytes(b"broken")
    source_path = tmp_path / "themes.json"
    source_path.write_text(
        json.dumps({"themes": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "predict-topic-label-model",
            "--model-dir",
            str(model_dir),
            "--source",
            str(source_path),
            "--output",
            str(tmp_path / "predictions.json"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "failed"
    assert "损坏" in output["error"] or "不完整" in output["error"]


def test_cli_returns_json_error_for_invalid_review_workbook(
    tmp_path: Path,
    capsys,
) -> None:
    source_path = tmp_path / "review.xlsx"
    source_path.write_bytes(b"not-an-excel-file")

    exit_code = main(
        [
            "train-topic-label-model",
            "--source",
            str(source_path),
            "--output-dir",
            str(tmp_path / "model"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "failed"
    assert "工作簿" in output["error"]
