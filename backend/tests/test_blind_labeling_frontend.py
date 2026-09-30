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


def test_blind_label_people_panel_only_shows_name_completed_annotations_and_last_activity() -> None:
    assert "查看每位标注员的完成标注量和最近活动" in FRONTEND
    assert "<th>标注员</th><th>总完成标注数</th><th>最后活动时间</th>" in FRONTEND
    assert "person.annotation_count || person.annotations || person.total_annotations || 0" in FRONTEND
    assert '<td colspan="3" class="blind-label-empty">' in FRONTEND
    assert ".blind-label-person-table{min-width:520px;table-layout:fixed}" in FRONTEND

def test_blind_label_date_filter_formats_dates_and_chains_selection() -> None:
    assert '@click="openBlindLabelDatePicker(\'start\')"' in FRONTEND
    assert '@click="openBlindLabelDatePicker(\'end\')"' in FRONTEND
    assert 'id="blind-label-start-date-native"' in FRONTEND
    assert 'id="blind-label-end-date-native"' in FRONTEND
    assert 'placeholder="年/月/日"' in FRONTEND
    assert "blindLabelDateDisplay: function(value)" in FRONTEND
    assert "handleBlindLabelDateChange: function(kind, event)" in FRONTEND
    assert "clearBlindLabelDate: function(kind)" in FRONTEND
    assert "self.openBlindLabelDatePicker('end', {auto:true})" in FRONTEND
    assert "typeof this.$nextTick === 'function'" in FRONTEND
    assert "this.applyBlindLabelOverviewFilters()" in FRONTEND
    assert "current.showPicker()" in FRONTEND


def test_blind_label_date_filter_has_loading_transition_and_reduced_motion_fallback() -> None:
    assert ':class="{applying:blindLabeling.overview.loading}"' in FRONTEND
    assert 'class="blind-label-filter-state"' in FRONTEND
    assert '@media(prefers-reduced-motion:reduce)' in FRONTEND


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


def test_blind_label_candidates_separate_reply_and_collapsible_detail() -> None:
    assert "blindLabelCandidateRecommendedReply" in FRONTEND
    assert "blindLabelCandidateDetailText" in FRONTEND
    assert '<details class="blind-label-candidate-details"' in FRONTEND
    assert 'aria-expanded="false"' in FRONTEND
    assert "blind-label-candidate-detail-text" in FRONTEND
    assert ".blind-label-candidate-headline .blind-label-candidate-rank" in FRONTEND
    assert "white-space:nowrap!important" in FRONTEND


def test_blind_label_embeds_the_work_order_chat_in_the_left_panel() -> None:
    assert '<section class="blind-label-chat-panel"' in FRONTEND
    assert '<iframe v-if="blindLabelChatUrl()"' in FRONTEND
    assert ':src="blindLabelChatUrl()"' in FRONTEND
    assert 'title="答疑平台聊天记录"' in FRONTEND
    assert "blindLabelChatUrl: function()" in FRONTEND
    assert "reloadBlindLabelChat: function()" in FRONTEND
    assert "chatFrameKey:0" in FRONTEND


def test_blind_label_does_not_open_or_position_a_separate_chat_window() -> None:
    start = FRONTEND.index("openBlindLabelAssignment: function(")
    end = FRONTEND.index("    closeBlindLabelDialog: function()", start)
    assignment_flow = FRONTEND[start:end]
    assert "prepareWorkOrderChat" not in assignment_flow
    assert "finishWorkOrderChat" not in assignment_flow
    assert "closeWorkOrderChatWindow" not in assignment_flow


def test_blind_label_candidate_cards_and_actions_have_visual_hierarchy() -> None:
    assert ".blind-label-candidate-section{padding:12px" in FRONTEND
    assert ".blind-label-candidate{border-color:#cfe3de" in FRONTEND
    assert ".blind-label-candidate-headline{background:#f1faf7" in FRONTEND
    assert ".blind-label-candidate-details>summary{background:#edf7f4" in FRONTEND
    assert ".blind-label-choice-buttons .btn{background:#eef9f6" in FRONTEND
    assert ".blind-label-mode-actions .btn{background:#fff3f1" in FRONTEND
    assert ".blind-label-mode-actions .btn.on-unhelpful" in FRONTEND
