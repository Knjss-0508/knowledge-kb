from pathlib import Path


FRONTEND = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")


def test_knowledge_flow_order_and_confidence_training_sections():
    sidebar = FRONTEND[FRONTEND.index("知识沉淀"):FRONTEND.index("</aside>", FRONTEND.index("知识沉淀"))]
    assert sidebar.index("运行监管") < sidebar.index("候选价值复核") < sidebar.index("置信度训练")
    assert "confidenceTraining" in FRONTEND
    assert "知识候选" in FRONTEND
    assert "置信度评测" in FRONTEND
    assert "训练候选" in FRONTEND
    assert "人工复核工作簿" in FRONTEND
    assert "/confidence-training/overview" in FRONTEND
    assert "/confidence-training/export?format=" in FRONTEND
    assert "production_auto_review_enabled:false" in FRONTEND
    assert "人工真值（来自候选复核）" in FRONTEND
    assert "自动模型评测" in FRONTEND
    assert "auto_training_job_enabled" in FRONTEND
    assert "createConfidenceTrainingJob" in FRONTEND
    assert "self.confidenceTraining.jobs = [job].concat" in FRONTEND
    assert "退化样本快速定位" in FRONTEND
    assert "查看数据详情" in FRONTEND
    assert "shadowRegressedRows" in FRONTEND
    assert "toggleShadowSampleDetail" in FRONTEND
    assert "退化样本处理" in FRONTEND
    assert "保存退化复核" in FRONTEND
    assert "/regressions/" in FRONTEND
    assert "生成 v1.1 候选 Prompt" in FRONTEND
    assert "canGeneratePromptRevision" in FRONTEND
    assert "样本原始数据" in FRONTEND
    assert "旧 Prompt 输出" in FRONTEND
    assert "候选 Prompt 输出" in FRONTEND
    assert "/integration/candidate-reviews/" in FRONTEND
    assert "inspectionOnly" in FRONTEND
    assert "shadowEvaluationVisibleRows" in FRONTEND
    assert "shadowEvaluationPageCount" in FRONTEND
    assert "confidenceTrainingPageCount" in FRONTEND
    assert "candidatePageSize" in FRONTEND
    assert "showAllItems" not in FRONTEND
    assert "人工标签（例如 worthy / unworthy）" not in FRONTEND
    assert "系统自动比较沉淀价值和草稿处理动作" in FRONTEND


def test_training_tab_binds_to_initialized_settings_state():
    assert "v-model=\"confidenceTraining.settings.training_candidate_collection_enabled\"" in FRONTEND
    assert "v-model=\"confidenceTraining.settings.training_snapshot_enabled\"" in FRONTEND
    assert "confidenceTraining: {tab:'candidates'" in FRONTEND
    assert "settings:{training_candidate_collection_enabled:true" in FRONTEND
