from pathlib import Path
import json
import shutil
import subprocess
import textwrap


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_model_revision_requires_human_apply_and_keeps_original_draft() -> None:
    assert "让模型修改知识草稿" in FRONTEND
    assert "只生成修订稿，不直接覆盖原稿" in FRONTEND
    assert "/model-revise" in FRONTEND
    assert "/model-revision:apply" in FRONTEND
    assert "采用模型修改并重新初标" in FRONTEND


def test_candidate_review_uses_draft_disposition_queue_and_update_date_filters() -> None:
    assert "基于转写草稿、来源证据和模型标注" in FRONTEND
    assert "grid-template-columns:repeat(9,minmax(0,1fr))" in FRONTEND
    assert "退回转写" in FRONTEND
    assert '<th style="width:175px">人工复核</th>' in FRONTEND
    assert "candidate-reviews:batch-annotate" in FRONTEND
    assert "批量标为不沉淀" in FRONTEND
    assert "quickAnnotateCandidateReview" not in FRONTEND
    assert '<th style="width:150px">快速标注</th>' not in FRONTEND
    assert "params.set('updated_from', filter.updatedFrom)" in FRONTEND
    assert "params.set('updated_to', filter.updatedTo)" in FRONTEND


def test_candidate_review_can_save_and_continue_without_blocking_success_alert() -> None:
    assert "saveCandidateReview(false,true)" in FRONTEND
    assert "保存并下一条" in FRONTEND
    assert "nextCandidateReviewId: function(currentId)" in FRONTEND
    assert "candidate-review-toast" in FRONTEND
    assert "alert('候选审核已保存。')" not in FRONTEND
    toast = FRONTEND.index('class="candidate-review-toast"')
    app_end = FRONTEND.index('  </div>\n  <script src="lib/vue.global.prod.js"></script>')
    assert toast < app_end


def test_candidate_review_list_reload_is_awaitable_for_save_and_next() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the candidate review behavior test"
    frontend_path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        const html = fs.readFileSync({json.dumps(str(frontend_path))}, 'utf8');
        const scripts = Array.from(
          html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/gi),
          function(match) {{ return match[1]; }}
        );
        const appSource = scripts.find(function(source) {{
          return source.indexOf('Vue.createApp({{') !== -1;
        }});
        let appOptions = null;
        const sandbox = {{
          window: {{KB_RUNTIME: {{apiBase: '', baseUrl: ''}}}},
          localStorage: {{getItem: function() {{ return ''; }}}},
          Vue: {{createApp: function(options) {{
            appOptions = options;
            return {{mount: function() {{ return null; }}}};
          }}}},
          fetch: function() {{
            return Promise.resolve({{
              ok: true,
              json: function() {{
                return Promise.resolve({{
                  items: [], total: 0, summary: {{}}, product_categories: []
                }});
              }}
            }});
          }},
          console: console,
          URL: URL,
          URLSearchParams: URLSearchParams,
          setTimeout: setTimeout,
          clearTimeout: clearTimeout,
          alert: function() {{}}
        }};
        vm.createContext(sandbox);
        vm.runInContext(appSource, sandbox);
        const context = {{
          candidateReviews: {{
            page: 1,
            pageSize: 20,
            loading: false,
            selected: [],
            items: [],
            productCategories: [],
            summary: {{}},
            filter: {{
              keyword: '', status: '', priorityOnly: false,
              deduplicationRequired: false, productCategory: '',
              annotationStatus: '', modelKnowledgeValue: '',
              updatedFrom: '', updatedTo: ''
            }}
          }},
          authHeaders: function() {{ return {{}}; }},
          reviewSelectable: function() {{ return false; }},
          formatDate: function() {{ return '-'; }}
        }};
        const reload = appOptions.methods.loadCandidateReviews.bind(context);
        const result = reload(false);
        assert(result && typeof result.then === 'function',
          '保存并下一条需要等待候选列表刷新，loadCandidateReviews 必须返回 Promise');
        result.then(function() {{ process.exit(0); }}).catch(function(error) {{
          console.error(error);
          process.exit(1);
        }});
        """
    )
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_candidate_review_uses_one_human_decision_for_legacy_gate_fields() -> None:
    assert "只需选择一次复核结论" in FRONTEND
    assert "复核说明（选填）" in FRONTEND
    assert "知识草稿处理" in FRONTEND
    assert "reviewModelDraftDispositionLabel" in FRONTEND
    assert "草稿处理由模型初标决定；人工仅确认是否值得沉淀。" in FRONTEND
    assert "draft_disposition" not in FRONTEND
    assert "form.knowledge_value === 'worthy' && form.draft_disposition === 'approved'" not in FRONTEND
    assert '<label class="fl">是否可用</label>' not in FRONTEND
    assert '<label class="fl">人工审核结论</label>' not in FRONTEND


def test_candidate_review_displays_and_edits_case_images_and_videos() -> None:
    assert "案例图片和视频" in FRONTEND
    assert "candidateReviews.form.mediaBlocks" in FRONTEND
    assert "candidateReviewMediaUrl(media)" in FRONTEND
    assert "removeCandidateReviewMedia(mediaIndex)" in FRONTEND
    assert "addCandidateReviewMedia('image')" in FRONTEND
    assert "addCandidateReviewMedia('video')" in FRONTEND
    assert "正文和案例媒体独立保存" in FRONTEND


def test_candidate_review_dialog_keeps_compact_training_and_media_typography() -> None:
    assert (
        ".candidate-training-toggle{display:inline-flex;align-items:center;gap:6px;"
        "color:#475467;font-size:12px;white-space:nowrap}"
        in FRONTEND
    )
    assert (
        ".candidate-media-empty{padding:16px;border:1px dashed #d0d5dd;"
        "border-radius:8px;background:#fafbfc;color:#98a2b3;font-size:12px;"
        "text-align:center}"
        in FRONTEND
    )
    assert ".candidate-media-toolbar{display:flex;align-items:center;gap:8px" in FRONTEND


def test_candidate_review_preserves_media_when_text_is_edited() -> None:
    assert "contentMediaBlocks: function(content)" in FRONTEND
    assert "candidateReviewContent: function(contentText, originalContent, mediaBlocks)" in FRONTEND
    assert "body.content = candidateContent" in FRONTEND
    assert "{blocks: form.contentText.trim()" not in FRONTEND


def test_candidate_review_final_layout_coexists_with_retrieval_review() -> None:
    assert 'class="candidate-filter-card"' in FRONTEND
    assert "审核队列 · 筛选条件" in FRONTEND
    assert '@click="resetCandidateReviewFilters"' in FRONTEND
    assert "resetCandidateReviewFilters: function()" in FRONTEND
    assert "openRetrievalReviewPage" in FRONTEND
    assert "最多各保留 TOP 3" in FRONTEND
