from pathlib import Path


FRONTEND = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")


def test_knowledge_flow_order_and_confidence_training_sections():
    sidebar = FRONTEND[FRONTEND.index("知识沉淀"):FRONTEND.index("</aside>", FRONTEND.index("知识沉淀"))]
    assert sidebar.index("运行监管") < sidebar.index("候选价值复核") < sidebar.index("标注质量评测")
    assert "confidenceTraining" in FRONTEND
    assert "冻结评测样本（人工真值基准）" in FRONTEND
    assert "冻结样本严格准确率" in FRONTEND
    assert "回归错误集（影子复跑退化样本）" in FRONTEND
    assert "退化样本已复核率" in FRONTEND
    assert "人工真值集" in FRONTEND
    assert "/confidence-training/overview" in FRONTEND
    assert "/confidence-training/export?format=" in FRONTEND
    assert "production_auto_review_enabled:false" in FRONTEND
    assert "冻结评测样本（人工真值基准）" in FRONTEND
    assert "候选 Prompt 严格准确率" in FRONTEND
    assert "auto_training_job_enabled" in FRONTEND
    assert "createConfidenceTrainingJob" in FRONTEND
    assert "self.confidenceTraining.jobs = [job].concat" in FRONTEND
    assert "回归错误集（影子复跑退化样本）" in FRONTEND
    assert "查看数据详情" in FRONTEND
    assert "shadowRegressedRows" in FRONTEND
    assert "return job.regression_rows||[];" in FRONTEND
    assert "job.regression_partitions" in FRONTEND
    assert "toggleShadowSampleDetail" in FRONTEND
    assert "确认 Prompt 退化" in FRONTEND
    assert "真值需要更正" in FRONTEND
    assert "排除并说明原因" in FRONTEND
    assert "/regressions/" in FRONTEND
    assert "生成修订候选 Prompt" in FRONTEND
    assert "canGeneratePromptRevision" in FRONTEND
    assert "样本原始数据" in FRONTEND
    assert "旧 Prompt 输出" in FRONTEND
    assert "候选 Prompt 输出" in FRONTEND
    assert "inspectionOnly" in FRONTEND
    assert "shadowEvaluationVisibleRows" in FRONTEND
    assert "shadowEvaluationPageCount" in FRONTEND
    assert "confidenceTrainingPageCount" in FRONTEND
    assert "candidatePageSize" in FRONTEND
    assert "showAllItems" not in FRONTEND
    assert "人工标签（例如 worthy / unworthy）" not in FRONTEND
    assert "版本仅展示系统已保存的 Prompt 快照" in FRONTEND
    assert "先跑验证集 {{job.validation_count}} 条" in FRONTEND
    assert "验证通过，进入测试集" in FRONTEND
    assert "批准后续新候选使用" in FRONTEND
    assert "?split=" in FRONTEND
    assert "classification:classification" in FRONTEND
    assert "优化草稿 Prompt" in FRONTEND
    assert "draft_generation" in FRONTEND
    assert "该 Prompt 已人工批准，仅应用于批准时间后新建的候选。" in FRONTEND
    assert "/confidence-training/evaluation-samples/snapshot" in FRONTEND
    assert "不可变快照" in FRONTEND
    assert "不训练或替换模型权重" in FRONTEND
    assert "confidenceTraining.overview.sample_provenance" in FRONTEND
    assert "comparison.candidate.sample_provenance" in FRONTEND
    assert "等待可回放的完整证据快照" in FRONTEND
    assert "草稿 Prompt 已接入 Answer Hub 离线回放" in FRONTEND
    assert "'draft_prompt_replay_required'" in FRONTEND
    assert "草稿 Prompt 回放结果仅供人工复核" in FRONTEND


def test_training_tab_binds_to_initialized_settings_state():
    assert "v-model=\"confidenceTraining.settings.training_candidate_collection_enabled\"" in FRONTEND
    assert "v-model=\"confidenceTraining.settings.training_snapshot_enabled\"" in FRONTEND
    assert "confidenceTraining: {tab:'overview'" in FRONTEND
    assert "settings:{training_candidate_collection_enabled:true" in FRONTEND


def test_model_connection_dialog_edits_the_internal_route_from_the_ui():
    assert "模型连接配置" in FRONTEND
    assert "openConfidenceModelConnection" in FRONTEND
    assert "applyConfidenceModelConnection" in FRONTEND
    assert "closeConfidenceModelConnection" in FRONTEND
    assert "confidenceModelConnectionPayload" in FRONTEND
    assert "confidenceModelConnectionKeyPlaceholder" in FRONTEND
    assert "confidenceModelConnectionKeySourceLabel" in FRONTEND
    assert "saveConfidenceModelConnection" in FRONTEND
    assert "testConfidenceModelConnection" in FRONTEND
    assert "resetConfidenceModelConnection" in FRONTEND
    assert "modelConnection:{open:false" in FRONTEND
    assert "'/confidence-training/model-connection'" in FRONTEND
    assert "/confidence-training/model-connection-test" in FRONTEND
    assert "模型地址（Base URL）" in FRONTEND
    assert "留空则保持不变" in FRONTEND
    assert "已保存，新的模型调用立即使用该配置。" in FRONTEND
    assert "clear_api_key" in FRONTEND
    assert "reset_to_environment:true" in FRONTEND
    assert "恢复使用环境变量" in FRONTEND
    assert "测试连接（用当前表单值）" in FRONTEND
    assert "保存并立即生效" in FRONTEND
    assert "v-model.trim=\"confidenceTraining.modelConnection.base_url\"" in FRONTEND
    assert "v-model.trim=\"confidenceTraining.modelConnection.api_key\"" in FRONTEND
    assert "api_key_masked" in FRONTEND
    # The dialog reads the key back masked only; it never renders a stored key.
    assert "type=\"password\"" in FRONTEND
    assert "测试模型连接" in FRONTEND
