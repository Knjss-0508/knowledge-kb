from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_admin_blind_labeling_splits_people_and_work_order_views() -> None:
    assert "人员统计</button>" in FRONTEND
    assert "已标注总览</button>" in FRONTEND
    assert "blindLabeling.tab==='people'" in FRONTEND
    assert "blindLabeling.tab==='workOrders'" in FRONTEND
    assert "blindLabeling.tab==='overview'" not in FRONTEND
    assert "id=\"blind-label-people-panel\"" in FRONTEND
    assert "id=\"blind-label-work-orders-panel\"" in FRONTEND


def test_admin_blind_labeling_defaults_to_people_and_keeps_shared_filters() -> None:
    assert "isBlindLabelOverviewTab: function()" in FRONTEND
    assert "this.blindLabeling.tab = this.isBlindLabelAdmin() ? 'people' : 'mine';" in FRONTEND
    assert "['people','workOrders'].indexOf(tab)" in FRONTEND
    assert "blindLabeling.overview.filters" in FRONTEND
    assert "changeBlindLabelOverviewPage(-1)" in FRONTEND


def test_blind_label_date_inputs_open_the_native_picker_on_input_click() -> None:
    assert FRONTEND.count('@click="openBlindLabelDatePicker"') == 2
    assert "openBlindLabelDatePicker: function(event)" in FRONTEND
    assert "input.showPicker()" in FRONTEND


def test_blind_label_accepts_one_referable_candidate_without_labeling_the_other_two() -> None:
    assert "不可参考原因" in FRONTEND
    assert "补充原因（选填）" in FRONTEND
    assert "blindLabelCandidateReasonRequired: function(candidate)" in FRONTEND
    assert "return this.blindLabelAllNotReferable();" in FRONTEND
    assert "blindLabelHasReferableCandidate: function()" in FRONTEND
    assert "return !this.blindLabelAllNotReferable() && !this.blindLabelHasReferableCandidate();" in FRONTEND
    assert "blindLabelMissingReasons: function()" in FRONTEND
    assert "reasonCode:self.blindLabelCandidateReasonCode(candidate)" in FRONTEND
    assert '@click="submitBlindLabelAssignment" :disabled="blindLabeling.saving"' in FRONTEND
    assert "blindLabeling.saving || blindLabelMissingLabels() || blindLabelMissingReasons()" not in FRONTEND
    assert "请选择一条最相关的候选“可参考”，或点击“均不可参考”。" in FRONTEND
    assert "if (!allNotReferable && value !== 'helpful') return items;" in FRONTEND


def test_blind_label_all_not_referable_requires_three_candidate_reasons() -> None:
    assert "均不可参考" in FRONTEND
    assert "setBlindLabelAllNotReferable: function(enabled)" in FRONTEND
    assert "current.allNotReferable = !!enabled;" in FRONTEND
    assert "均不可参考模式下，还需为候选 " in FRONTEND
    assert "三条候选均不可参考，已完成三条原因判断，可以提交。" in FRONTEND
    assert "knowledge_exists_not_recalled" in FRONTEND  # 保留旧数据兼容的工单级字段映射
    assert "knowledge_missing" in FRONTEND
    assert "unable_to_judge" in FRONTEND
    assert "taskReasonCode:String(current.taskReasonCode || '').trim()" in FRONTEND
    assert "taskReasons:[]" in FRONTEND
    assert "本工单无有效候选原因</h4>" in FRONTEND
    assert "taskReasonCode:taskReasonCode" in FRONTEND


def test_blind_label_claim_is_explicit_and_completed_batch_offers_next_batch() -> None:
    assert "/blind-labeling/my-batch:claim?target_count=50" in FRONTEND
    assert "{method:'POST'" in FRONTEND
    assert "blindLabeling.batch.status==='completed'?'领取下一批':'领取 50 条任务'" in FRONTEND
    assert "本批 50 条已完成，可点击“领取下一批”。" in FRONTEND
    assert "return next ? self.openBlindLabelAssignment(next) : null;" in FRONTEND


def test_blind_label_annotators_cannot_manually_release_tasks() -> None:
    assert "releaseBlindLabelAssignment: function()" not in FRONTEND
    assert "blindLabeling.releasing" not in FRONTEND
    assert "释放任务" not in FRONTEND
    assert "/blind-labeling/assignments/' + encodeURIComponent(id) + '/release" not in FRONTEND
    assert "任务自领取起 24 小时未提交将由系统自动回收。" in FRONTEND
    assert "released:'已自动回收'" in FRONTEND
    assert FRONTEND.count("indexOf('超时自动回收')") == 2


def test_blind_label_batch_displays_backend_auto_reclaimed_count() -> None:
    assert "released:0" in FRONTEND
    assert "['released','released_count','releasedCount']" in FRONTEND
    assert "released:released" in FRONTEND
    assert "系统自动回收</span><b>{{blindLabeling.batch.released || 0}}</b>" in FRONTEND
    assert "blindLabelCountByStatus('released')" not in FRONTEND
