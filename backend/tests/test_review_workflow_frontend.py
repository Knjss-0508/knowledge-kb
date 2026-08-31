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
