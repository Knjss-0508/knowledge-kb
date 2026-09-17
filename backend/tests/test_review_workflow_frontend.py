import json
import subprocess
from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def _run_frontend_behavior(body: str) -> None:
    inline_script = FRONTEND.rsplit("<script>", 1)[1].split(
        "</script>",
        1,
    )[0]
    harness = f"""
const source = {json.dumps(inline_script, ensure_ascii=False)};
let captured = null;
globalThis.window = {{KB_RUNTIME: {{apiBase: '', baseUrl: ''}}}};
globalThis.localStorage = {{getItem: function() {{ return ''; }}, setItem: function() {{}}, removeItem: function() {{}}}};
globalThis.document = {{
  addEventListener: function() {{}},
  createElement: function() {{ return {{innerHTML: '', childNodes: []}}; }}
}};
globalThis.Vue = {{
  createApp: function(options) {{
    captured = options;
    return {{mount: function() {{ return {{}}; }}}};
  }}
}};
new Function(source)();
if (!captured || !captured.methods) throw new Error('Vue methods not captured');
const vm = Object.assign({{}}, captured.methods);
const assert = function(condition, message) {{
  if (!condition) throw new Error(message);
}};
{body}
"""
    result = subprocess.run(
        ["node", "-"],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_knowledge_review_exposes_a_quick_review_desk() -> None:
    assert "<!-- 快速审核工作台 -->" in FRONTEND
    assert 'class="review-desk"' in FRONTEND
    assert "进入审核工作台" in FRONTEND
    assert "openReviewDesk: function(item)" in FRONTEND
    assert "loadReviewDeskDetail: function(row, requestId)" in FRONTEND
    assert "进入完整审核" in FRONTEND


def test_quick_review_supports_continuous_actions_and_keyboard_shortcuts() -> None:
    assert "通过并下一条" in FRONTEND
    assert "废弃并下一条" in FRONTEND
    assert "approveQuickReview: function(moveNext)" in FRONTEND
    assert "deprecateQuickReview: function(moveNext)" in FRONTEND
    assert "self.navigateReviewDesk(e.key === 'ArrowLeft' ? -1 : 1)" in FRONTEND
    assert "self.approveQuickReview(true)" in FRONTEND
    assert "if (!isFinite(index)) index = -1;" in FRONTEND
    assert "var currentIndex = Number(this.reviewDesk && this.reviewDesk.currentIndex);" in FRONTEND


def test_review_issue_summary_can_expand_in_list_and_desk() -> None:
    assert "reviewIssueExpandedIds" in FRONTEND
    assert "toggleReviewIssue: function(item)" in FRONTEND
    assert "{{isReviewIssueExpanded(r)?'收起原因':'查看原因'}}" in FRONTEND
    assert "{{reviewDesk.issueExpanded?'收起原因':'展开原因'}}" in FRONTEND
    assert "reviewIssueDetail: function(item)" in FRONTEND
    assert "detailFallback" in FRONTEND
    assert "暂不能修改或发布" in FRONTEND


def test_batch_review_keeps_a_persistent_failure_result_panel() -> None:
    assert "本次批量审核结果" in FRONTEND
    assert "knowledgeBatchResult" in FRONTEND
    assert "knowledgeBatchFailureResults: function()" in FRONTEND
    assert "openReviewDeskById(result.knowledge_id)" in FRONTEND
    assert "批量审核完成：通过 " not in FRONTEND


def test_quick_review_respects_deprecate_permission_and_source_rules() -> None:
    assert "v-if=\"can('knowledge:deprecate') && reviewDesk.detail && reviewDesk.detail.knowledge_origin!=='model_configuration'\"" in FRONTEND
    assert "if (!this.can('knowledge:deprecate') || detail.knowledge_origin === 'model_configuration')" in FRONTEND


def test_quick_review_renders_text_and_media_blocks() -> None:
    assert "contentBlocks:[]" in FRONTEND
    assert ".review-desk-mask{align-items:center;justify-content:center" in FRONTEND
    assert "reviewDeskContentBlocks(data.content, data.media)" in FRONTEND
    assert "reviewDeskContentBlocks: function(content, mediaList)" in FRONTEND
    assert "reviewDeskContentTextBlocks: function(value)" in FRONTEND
    assert "<img v-if=\"block.type==='image' && !block.load_error\"" in FRONTEND
    assert "@error=\"reviewDeskMediaError(block)\"" in FRONTEND
    assert "source.external_url || source.url || source.src" in FRONTEND
    assert "media.file_path || media.filename || media.url" in FRONTEND
    assert "reviewDeskContentPreview" not in FRONTEND


def test_quick_review_media_sources_are_normalized_for_display() -> None:
    _run_frontend_behavior(
        r"""
vm.richTextToPlainText = function(value) { return String(value || '').replace(/<[^>]*>/g, ''); };
vm.trimMediaPlaceholderLines = function(value) { return String(value || '').trim(); };
const media = [{id: 'media-1', filename: 'local.png', file_path: '/uploads/local.png', alt: '本地图', caption: '本地说明'}];
const blocks = vm.reviewDeskContentBlocks({blocks: [
  {type: 'text', value: '前文'},
  {type: 'image', media_id: 'media-1'},
  {type: 'text', value: '后文'}
]}, media);
assert(blocks.length === 3, 'text and media blocks should keep order');
assert(blocks[1].type === 'image', 'media block type should be image');
assert(blocks[1].url === '/uploads/local.png', 'media id should resolve to uploads URL');
assert(blocks[1].alt === '本地图' && blocks[1].caption === '本地说明', 'media metadata should be retained');
const legacy = vm.reviewDeskContentBlocks('<img src="https://cdn.example.com/a.png">\n- /uploads/local.png', media);
assert(legacy.filter(function(item) { return item.type === 'image'; }).length === 2, 'legacy image forms should render as image blocks');
assert(legacy[0].url === 'https://cdn.example.com/a.png', 'HTTPS image should retain CDN URL');
assert(legacy[1].url === '/uploads/local.png', 'legacy local image should resolve to uploads URL');
const unsafe = vm.reviewDeskContentBlocks('[img:http://insecure.example.com/a.png]', media);
assert(!unsafe.some(function(item) { return item.type === 'image' && item.url; }), 'insecure image URL must not become a rendered media URL');
""",
    )


def test_candidate_review_restores_compact_two_column_layout() -> None:
    assert ".review-dialog{width:min(1160px,100%)}" in FRONTEND
    assert ".review-detail-grid{display:grid;grid-template-columns:minmax(0,.88fr) minmax(0,1.12fr)" in FRONTEND
    assert ".review-content-preview{max-height:220px;overflow:auto" in FRONTEND
    assert ".review-model-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in FRONTEND
    assert ".review-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in FRONTEND
    assert "@media(min-width:900px){.review-dialog .review-detail-grid{grid-template-columns:minmax(300px,.88fr) minmax(420px,1.12fr)}}" in FRONTEND


def test_quick_review_desk_uses_large_centered_canvas() -> None:
    assert ".review-desk{width:min(1440px,calc(100% - 64px));height:auto;max-height:calc(100vh - 48px)" in FRONTEND
    assert ".review-desk-b{flex:0 1 auto;max-height:calc(100vh - 210px);padding:28px 30px}" in FRONTEND
    assert ".review-desk-content{max-height:min(560px,56vh);padding:18px;font-size:14px;line-height:1.8}" in FRONTEND
    assert ".review-desk-content-media-frame{min-height:300px}" in FRONTEND
    assert "@media(min-width:761px) and (max-width:1040px){.review-desk{width:calc(100% - 32px)}" in FRONTEND
    assert ".review-desk-f .btn{min-height:36px;padding:6px 14px;font-size:13px}" in FRONTEND


def test_quick_review_can_compare_and_confirm_deduplication_matches() -> None:
    assert "对比并确认" in FRONTEND
    assert "判定不同的原因（选填）" in FRONTEND
    assert "dedupCompare.loading || dedupCompare.submitting || !dedupCompare.reason.trim()" not in FRONTEND
    assert "<b>适用品类：</b>{{applicableCategoryText(dedupCompare.candidate) || '未设置'}}" in FRONTEND
    assert "<b>适用品类：</b>{{applicableCategoryText(dedupCompare.target) || '未设置'}}" in FRONTEND
    assert "var cache = this.manhattanCacheFor(item && item.business_type);" in FRONTEND
    assert "if (String(options[index].value) === categoryId) return String(options[index].label || categoryId).trim();" in FRONTEND
    assert "this.loadManhattanOptions(detail.business_type);" in FRONTEND
    assert "applicable_categories: detail.applicable_categories || []" in FRONTEND
    assert "&& entry.verdict === 'different'\n            && String(entry.reason || '').trim();" not in FRONTEND
    assert "openReviewDeskDedupCompare: function(match)" in FRONTEND
    assert "this.dedupCompare.sourceKnowledgeId = detail.id;" in FRONTEND
    assert "feedbackEnabled = !!(this.can('knowledge:approve') && detail.id)" in FRONTEND
    assert "var sourceKnowledgeId = self.dedupCompare.sourceKnowledgeId || self.eid;" in FRONTEND
    assert "self.reviewDesk.detail.deduplication_metadata = data.deduplication_metadata" in FRONTEND


def test_full_image_preview_supports_zoom_controls() -> None:
    assert 'aria-label="缩小图片"' in FRONTEND
    assert 'aria-label="放大图片"' in FRONTEND
    assert '@wheel.prevent="zoomFullPreview($event.deltaY < 0 ? 1 : -1)"' in FRONTEND
    assert "setFullPreviewZoom: function(zoom)" in FRONTEND
    _run_frontend_behavior(
        r"""
vm.normalizePreviewUrl = function(value) { return value; };
vm.full = {show: false, src: '', isVideo: false, error: false, zoom: 1};
vm.showFull('https://cdn.example.com/original.png', false);
assert(vm.full.show && vm.full.zoom === 1, 'opening an image should reset zoom');
vm.zoomFullPreview(1);
assert(vm.full.zoom === 1.25, 'zoom in should add one step');
vm.zoomFullPreview(-10);
assert(vm.full.zoom === 0.5, 'zoom should have a lower bound');
vm.setFullPreviewZoom(9);
assert(vm.full.zoom === 4, 'zoom should have an upper bound');
vm.resetFullPreviewZoom();
assert(vm.full.zoom === 1, 'reset should restore default zoom');
""",
    )


def test_all_business_filter_keeps_scope_fields_visible_and_aggregates_options() -> None:
    assert '<div class="tab-row" v-if="f.business_type">' not in FRONTEND
    assert '<div class="filter-applicability-grid" v-if="f.business_type">' not in FRONTEND
    assert "self.businessTypes.forEach(function(businessType)" in FRONTEND
    _run_frontend_behavior(
        r"""
vm.f = {business_type: '', applicableCategoryIds: [], brandIds: []};
vm.businessTypes = [{value: 'self_operated'}, {value: 'aggregated'}];
vm.mhCaches = {
  self_operated: {
    applicable_categories: [{categoryId: 'phone', categoryName: '手机'}],
    brands_by_category: {phone: [{brandId: 'apple', brandName: '苹果'}]},
    models: [{modelId: 'iphone-15', modelName: 'iPhone 15', categoryId: 'phone', brandId: 'apple'}]
  },
  aggregated: {
    applicable_categories: [{categoryId: 'tablet', categoryName: '平板'}],
    brands_by_category: {tablet: [{brandId: 'huawei', brandName: '华为'}]},
    models: [{modelId: 'matepad', modelName: 'MatePad', categoryId: 'tablet', brandId: 'huawei'}]
  }
};
assert(vm.listFilterOptions('applicableCategories').length === 2, 'all business should aggregate categories');
vm.f.applicableCategoryIds = ['tablet'];
assert(vm.listFilterOptions('brands')[0].value === 'huawei', 'selected category should use its business cache');
vm.f.brandIds = ['huawei'];
assert(vm.listFilterOptions('models')[0].value === 'matepad', 'selected brand should use its business cache');
""",
    )


def test_wecom_drive_video_opens_in_browser_session() -> None:
    assert "isWeComDriveUrl: function(value)" in FRONTEND
    assert "openWeComDriveVideo: function(src)" in FRONTEND
    assert "在企业微信中打开" in FRONTEND
    _run_frontend_behavior(
        r"""
let opened = null;
globalThis.window.open = function(src, target) { opened = {src: src, target: target}; return opened; };
globalThis.alert = function() { throw new Error('should not alert when browser tab opens'); };
vm.normalizePreviewUrl = function(value) { return value; };
vm.full = {show: false, src: '', isVideo: false, error: false, zoom: 1};
vm.showFull('https://drive.weixin.qq.com/example-video', true);
assert(opened && opened.src === 'https://drive.weixin.qq.com/example-video', 'WeCom drive video should open in a new browser tab');
assert(opened.target === '_blank', 'WeCom drive video should not be embedded');
assert(vm.full.show === false, 'WeCom drive video should not enter the embedded preview');
""",
    )

