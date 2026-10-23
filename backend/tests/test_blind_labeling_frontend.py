from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def _template_end(source: str, start: int) -> int:
    """Return the index just past the `</template>` that closes the one at `start`."""
    index = start
    depth = 0
    while index < len(source):
        opened = source.find("<template", index)
        closed = source.find("</template>", index)
        if closed == -1:
            break
        if opened != -1 and opened < closed:
            depth += 1
            index = opened + len("<template")
            continue
        depth -= 1
        if depth == 0:
            return closed + len("</template>")
        index = closed + len("</template>")
    return len(source)


def test_admin_blind_labeling_splits_people_and_work_order_views() -> None:
    assert "人员统计</button>" in FRONTEND
    assert "已标注总览</button>" in FRONTEND
    assert "blindLabeling.tab==='people'" in FRONTEND
    assert "blindLabeling.tab==='workOrders'" in FRONTEND
    assert "blindLabeling.tab==='overview'" not in FRONTEND
    assert "id=\"blind-label-people-panel\"" in FRONTEND
    assert "id=\"blind-label-work-orders-panel\"" in FRONTEND


def test_blind_label_frontend_uses_five_annotators_and_unanimous_consensus() -> None:
    assert "每工单最多 5 名标注员 · 全员一致自动通过" in FRONTEND
    assert "Math.min(5, Math.max(1" in FRONTEND
    assert "全员一致或已仲裁" in FRONTEND
    assert "独立标注存在分歧" in FRONTEND
    assert "待完成 ' + required + ' 人独立标注后计算共识。" in FRONTEND


def test_admin_blind_labeling_defaults_to_people_and_keeps_shared_filters() -> None:
    assert "isBlindLabelOverviewTab: function()" in FRONTEND
    assert "this.blindLabeling.tab = this.isBlindLabelAdmin() ? 'people' : 'mine';" in FRONTEND
    # 管理员可选页签 = 两个管理员总览页 + 共用的「标注排行」。
    assert "['people','workOrders','ranking'].indexOf(tab)" in FRONTEND
    assert "blindLabeling.overview.filters" in FRONTEND
    assert "changeBlindLabelOverviewPage(-1)" in FRONTEND


def test_blind_label_people_panel_only_shows_name_completed_annotations_and_last_activity() -> None:
    assert "查看每位标注员的完成标注量和最近活动" in FRONTEND
    assert "<th>标注员</th><th>总完成标注数</th><th>最后活动时间</th>" in FRONTEND
    assert "person.annotation_count || person.annotations || person.total_annotations || 0" in FRONTEND
    assert '<td colspan="3" class="blind-label-empty">' in FRONTEND
    assert ".blind-label-person-table{min-width:900px}" in FRONTEND

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
    assert "return next ? self.openBlindLabelAssignment(next) : null;" in FRONTEND


def test_blind_label_batch_size_comes_from_loaded_items_not_the_fixed_claim_size() -> None:
    # 固定批次口径只用于「领取」动作，界面展示必须回落到真实装载条数。
    assert "blindLabelBatchSize: function(batch)" in FRONTEND
    assert "var realItems = Math.max(completed + inProgress, assigned, loaded);" in FRONTEND
    assert "{{blindLabeling.batch.completed || 0}}/{{blindLabelBatchSize()}}" in FRONTEND
    assert ">当前 50 条批次<" not in FRONTEND
    assert "{{blindLabeling.batch.total || 50}}" not in FRONTEND
    assert "本批 50 条已完成" not in FRONTEND
    assert "var total = Number(this.blindLabeling.batch.total) || 50" not in FRONTEND


def test_blind_label_batch_progress_meta_stays_consistent_with_the_displayed_size() -> None:
    # 进度明细必须和头部 N/M 同口径：后端 pending 是「50 - 未回收条数」的推算值，
    # 前端按真实条数重算剩余名额，且已完成的批次不再展示剩余名额。
    assert "blindLabelBatchEmptyHint: function()" in FRONTEND
    assert "if (!claimed) return '本批没有可标注的工单" in FRONTEND
    assert "if (batch.status === 'completed') return '本批 ' + size + ' 条已完成，可点击“领取下一批”。';" in FRONTEND
    assert "被系统回收，等待新样本入库后点击“补充/刷新任务”" in FRONTEND
    assert "</span><span>待处理 <b>" not in FRONTEND
    assert "{{blindLabeling.batch.pending || 0}}" not in FRONTEND
    assert '<span>进行中 <b>{{blindLabeling.batch.in_progress || 0}}</b></span><span v-if="blindLabelBatchPending()">剩余名额 <b>{{blindLabelBatchPending()}}</b></span>' in FRONTEND
    assert "if (batch.status === 'completed') return 0;" in FRONTEND


def test_blind_label_batch_progress_card_is_mine_only() -> None:
    # 「我的已标注」不能沿用待标批次的进度卡。
    assert "<template v-if=\"blindLabeling.tab==='mine'\">" in FRONTEND
    assert 'class="blind-label-progress-card"' in FRONTEND
    assert "if (tab === 'completed') this.blindLabeling.batch = this.blindLabelEmptyBatch();" in FRONTEND
    assert "if (self.blindLabeling.tab === 'completed') self.blindLabeling.batch = self.blindLabelEmptyBatch();" in FRONTEND
    assert "blindLabelEmptyBatch: function()" in FRONTEND
    start = FRONTEND.index("<template v-if=\"blindLabeling.tab==='mine' || blindLabeling.tab==='completed'\">")
    end = FRONTEND.index("<template v-if=\"isBlindLabelOverviewTab()\">", start)
    flow = FRONTEND[start:end]
    guard = "<template v-if=\"blindLabeling.tab==='mine'\">"
    assert flow.count(guard) == 1
    # 进度卡必须落在 mine 守卫内部；守卫之外的共用区域（含「我的已标注」）不允许再出现。
    guard_start = flow.index(guard)
    guard_end = _template_end(flow, guard_start)
    assert 'class="blind-label-progress-card"' in flow[guard_start:guard_end]
    assert 'class="blind-label-progress-card"' not in flow[guard_end:]


def test_blind_label_my_annotations_filters_by_activity_date_and_defaults_to_today() -> None:
    # 「我的已标注」默认只看当天，可切换范围查看全部标注历史。
    assert "mineFilter:{preset:'today',dateFrom:'',dateTo:''}" in FRONTEND
    assert "blindLabelDateString: function(offsetDays)" in FRONTEND
    assert "blindLabelMineRange: function()" in FRONTEND
    assert "blindLabelMineRangeLabel: function()" in FRONTEND
    assert "blindLabelMineFilterEmptyHint: function()" in FRONTEND
    assert "setBlindLabelMinePeriod: function(preset)" in FRONTEND
    assert "applyBlindLabelMineDates: function()" in FRONTEND
    assert "if (preset === 'all') return {from:'',to:''};" in FRONTEND
    assert "params.set('start_date', range.from);" in FRONTEND
    assert "params.set('end_date', range.to);" in FRONTEND
    assert 'v-if="blindLabeling.tab===\'completed\'" class="blind-label-mine-filter"' in FRONTEND
    assert '@click="setBlindLabelMinePeriod(option.value)"' in FRONTEND
    assert "blindLabelMineFilterEmptyHint()" in FRONTEND
    assert "标注活动时间 " in FRONTEND
    assert ".blind-label-mine-filter{display:flex" in FRONTEND
    # 时间筛选只在「我的已标注」出现，待标页签不受影响
    assert ":blindLabelMineFilterEmptyHint()" in FRONTEND
    assert "暂无已标注工单。完成提交后会显示在这里。" in FRONTEND


def test_blind_label_my_annotations_paginates_instead_of_truncating_to_200() -> None:
    # 「我的已标注」曾经固定第一页 200 条，超过 200 条的历史标注永远看不到。
    assert "params.set('page_size', '200');" not in FRONTEND
    assert "params.set('page', '1');\n        params.set('page_size', '200');" not in FRONTEND
    assert "params.set('page', String(Math.max(1, Number(this.blindLabeling.pagination.page) || 1)));" in FRONTEND
    assert "params.set('page_size', String(pageSize));" in FRONTEND
    assert "blindLabelMineTotalPages: function()" in FRONTEND
    assert "changeBlindLabelMinePage: function(delta)" in FRONTEND
    assert "changeBlindLabelMinePageSize: function(event)" in FRONTEND
    # 条数必须取服务端全量 total，不能再用当前页长度冒充总数。
    assert "blindLabelListTotal: function()" in FRONTEND
    assert "if (this.blindLabeling.tab === 'completed') return Number(this.blindLabeling.pagination.total) || 0;" in FRONTEND
    # 条数单位必须写「个工单」：这个 total 是工单级派发数，不是候选标注条数，
    # 否则会被读成与「标注排行」的标注条数同一口径（两者相差 2 倍多）。
    assert "共 <b>{{blindLabelListTotal()}}</b> 个工单" in FRONTEND
    assert "页 · 共 {{blindLabeling.pagination.total}} 个工单" in FRONTEND
    assert "共 <b>{{blindLabelListTotal()}}</b> 条" not in FRONTEND
    assert "页 · 共 {{blindLabeling.pagination.total}} 条" not in FRONTEND
    # 旧写法把「当前页长度」无条件当成总数；现在只在待标页签保留该口径。
    assert (
        "self.blindLabeling.pagination.total = self.blindLabeling.assignments.length;\n          self.blindLabeling.updatedAt"
        not in FRONTEND
    )
    assert "pagination.total = serverTotal;" in FRONTEND
    assert "pagination.totalPages = Math.max(1, Number(data.total_pages) || Math.ceil(serverTotal / pageSize));" in FRONTEND
    # 分页条只在「我的已标注」出现，并且能切换每页条数。
    assert "blindLabeling.tab==='completed' && blindLabelMineTotalPages() > 1" in FRONTEND
    assert '@click="changeBlindLabelMinePage(-1)"' in FRONTEND
    assert '@click="changeBlindLabelMinePage(1)"' in FRONTEND
    assert '@change="changeBlindLabelMinePageSize"' in FRONTEND
    assert 'aria-label="每页条数"' in FRONTEND
    # 默认每页 50 条（服务端分组取数），避免一次拉太多导致查询变慢。
    assert "pagination:{page:1,size:50,total:0,totalPages:1}," in FRONTEND
    assert "pagination.size = Math.max(1, value || Number(pagination.size) || 50);" in FRONTEND
    assert "var pageSize = Math.max(1, Number(this.blindLabeling.pagination.size) || 50);" in FRONTEND
    # 切换时间范围要回到第一页，否则会停在上一个范围的页码上。
    assert FRONTEND.count("this.blindLabeling.pagination.page = 1;") >= 3


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


def test_blind_label_ranking_tab_is_available_to_annotators_and_admins() -> None:
    """「标注排行」页签对标注员和管理员都要出现（截图里那是标注员视角）。"""

    assert "标注排行</button>" in FRONTEND
    assert "id=\"blind-label-ranking-tab\"" in FRONTEND
    assert "id=\"blind-label-ranking-panel\"" in FRONTEND
    assert "blindLabeling.tab==='ranking'" in FRONTEND
    # 两个角色的可选页签都包含 ranking。
    assert "['people','workOrders','ranking'].indexOf(tab)" in FRONTEND
    assert "['mine','completed','ranking'].indexOf(tab)" in FRONTEND
    # 它不属于管理员专属总览页签，否则标注员会被 isBlindLabelOverviewTab 挡住。
    assert "isBlindLabelRankingTab: function()" in FRONTEND
    assert "return this.blindLabeling.tab === 'ranking';" in FRONTEND


def test_blind_label_ranking_panel_shows_name_grouped_counts_descending() -> None:
    """按人名分组，列出标注总数并带名次，从大到小排列由后端保证。"""

    # 从页签按钮定位到面板，避免只截到按钮本身。
    start = FRONTEND.index("id=\"blind-label-ranking-tab\"")
    end = FRONTEND.index("id=\"blind-label-ranking-panel\"")
    end = FRONTEND.index("</table>", end)
    panel = FRONTEND[start:end]

    assert "标注排行" in panel
    assert ">排名</th>" in panel
    # 「标注条数」是候选级判断条数，「完成工单数」是工单级派发数，单位必须在表头
    # 就区分开，并且两列都能悬停看到口径。
    assert (
        "<th>标注员</th>"
        "<th :title=\"blindLabelRankingHint('annotations')\">标注条数（候选判断）"
        "<span class=\"blind-label-hint-mark\">?</span></th>"
        "<th :title=\"blindLabelRankingHint('completed')\">完成工单数"
        "<span class=\"blind-label-hint-mark\">?</span></th>"
    ) in panel
    assert "<th>标注总数</th>" not in panel
    assert "v-for=\"person in blindLabeling.ranking.people\"" in panel
    assert "{{person.rank}}" in panel
    assert "blindLabelRankingAnnotations(person)" in panel
    assert "当前时间范围内还没有标注记录。" in panel


def test_blind_label_ranking_loader_calls_the_ranking_endpoint() -> None:
    assert "loadBlindLabelRanking: function()" in FRONTEND
    assert "'/blind-labeling/ranking?'" in FRONTEND
    assert "blindLabelRankingPeriods: function()" in FRONTEND
    assert "setBlindLabelRankingPeriod: function(period)" in FRONTEND
    # 打开排行页签时必须真的拉数据，而不是沿用总览/个人列表的加载器。
    assert "if (this.isBlindLabelRankingTab()) this.loadBlindLabelRanking();" in FRONTEND
    assert ".blind-label-ranking-table{min-width:860px}" in FRONTEND


def test_blind_label_ranking_explains_the_completion_rate_denominator() -> None:
    """完成率的分母不含「已回收」，必须显式展示并解释，否则 100% 会被误读。

    用户反馈：「这个完成率指的是什么」。真实口径是
    ``完成工单数 ÷ 实际派发数（不含被回收）``，所以邓志成 100% 但实际被回收了 98 单，
    刘永冰 88.2% 却是 120/136。列表里必须同时给出「已回收」，并说明分母口径。
    """

    # 列与取值
    assert ">已回收<span class=\"blind-label-hint-mark\">?</span></th>" in FRONTEND
    assert "blindLabelRankingReleased(person)" in FRONTEND
    assert "blindLabelRankingReleased: function(person)" in FRONTEND
    # 汇总卡也给出总回收量
    assert "已回收派发" in FRONTEND
    assert "blindLabeling.ranking.summary.released_count" in FRONTEND
    # 口径说明（悬停提示 + 段落说明）必须写明分母不含回收
    assert "blindLabelRankingHint: function(field)" in FRONTEND
    assert "不含已回收的派发" in FRONTEND
    assert "不代表没有被回收" in FRONTEND
    # 空态跨列数要跟着列数从 8 调到 9，否则表格错位
    assert '<td colspan="9" class="blind-label-empty">' in FRONTEND


def test_blind_label_ranking_explains_annotation_count_vs_work_order_count() -> None:
    """「标注条数」与「完成工单数」的口径必须在悬停里讲清楚。

    用户看到排行 675、我的已标注 300，会以为丢了数据。真实原因是
    675 是候选级判断条数（1 个工单最多 3 条候选），300 是工单数。
    """

    assert "if (field === 'annotations') {" in FRONTEND
    assert "1 个工单最多 3 条候选" in FRONTEND
    assert "if (field === 'completed') {" in FRONTEND
    assert "按工单计，不按候选计" in FRONTEND
    # 汇总卡与段落说明也要跟着改，不能同一页出现两种叫法
    assert '<span class="card-label">标注条数</span>' in FRONTEND
    assert "{{blindLabeling.ranking.summary.annotations || 0}} 条候选判断" in FRONTEND


def test_blind_label_ranking_panel_is_not_nested_inside_the_admin_only_wrapper() -> None:
    """排行面板必须是管理员总览模板的**兄弟节点**。

    线上现象：标注员点开「标注排行」页签，标签高亮但面板整块空白 ——
    因为面板被写进了 ``<template v-if="isBlindLabelOverviewTab()">`` 里面，
    而该条件对标注员恒为假（它要求 retrieval:label_overview），
    于是面板连同汇总卡、时间范围和表格一起被整个条件渲染掉。
    页签本身在两个角色下都渲染，所以看起来「点了没反应」。
    """

    admin_wrapper_start = FRONTEND.index("<template v-if=\"isBlindLabelOverviewTab()\">")
    admin_wrapper_end = _template_end(FRONTEND, admin_wrapper_start)
    ranking_start = FRONTEND.index("<template v-if=\"blindLabeling.tab==='ranking'\">")

    # 关键断言：排行模板不能落在管理员专属模板的区间内。
    assert not (admin_wrapper_start < ranking_start < admin_wrapper_end), (
        "排行面板被嵌套在 isBlindLabelOverviewTab() 里，标注员将看不到任何内容"
    )

    # 面板与页签按钮仍要存在且各自完整。
    assert "id=\"blind-label-ranking-tab\"" in FRONTEND
    assert "id=\"blind-label-ranking-panel\"" in FRONTEND
    assert FRONTEND[ranking_start:].index("</template>") > 0
