from pathlib import Path
import json
import shutil
import subprocess
import textwrap


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_model_revision_requires_human_apply_and_keeps_original_draft() -> None:
    assert "重新生成模型修订稿" in FRONTEND
    assert "修订仅待人工采用，不直接覆盖原稿" in FRONTEND
    assert "初标要求修订时系统会自动尝试；这里可重试。" in FRONTEND
    assert "/model-revise" in FRONTEND
    assert "/model-revision:apply" in FRONTEND
    assert "采用模型修改并重新初标" in FRONTEND


def test_batch_model_annotation_requests_full_revision_and_submission_workflow() -> None:
    assert "批量模型标注并送审" in FRONTEND
    assert "apply_model_revisions:true" in FRONTEND
    assert "submit_ready:true" in FRONTEND
    assert "采用修订 " in FRONTEND
    assert "重新初标 " in FRONTEND
    assert "送审成功 " in FRONTEND


def test_batch_model_annotation_sends_full_workflow_flags() -> None:
    node = shutil.which("node")
    assert node
    script = r"""
        const assert = require('assert');
        const vm = require('vm');
        const source = require('fs').readFileSync(0, 'utf8');
        const start = source.indexOf('    batchModelAnnotateCandidateReviews: function(');
        const end = source.indexOf('    annotateCandidateReviewIds: function(', start);
        const expression = source.slice(start, end).trim()
          .slice('batchModelAnnotateCandidateReviews'.length + 2).replace(/,$/, '');
        const requests = [], alerts = [];
        const sandbox = {
          API:'',
          confirm:() => true,
          alert:message => alerts.push(message),
          fetch:(url, options) => {
            requests.push({url, options});
            return Promise.resolve({ok:true,json:() => Promise.resolve({
              updated:1,failed:0,revisions_applied:1,reannotated:1,
              submitted:1,reused:0,submission_failed:0,
              results:[{ingestion_id:'ing-1',status:'updated',
                review_status:'submitted',submission_status:'submitted'}]
            })});
          }
        };
        const method = vm.runInNewContext('(' + expression + ')', sandbox);
        const context = {
          candidateReviews:{selected:['ing-1'],modelAnnotating:false},
          authHeaders:headers => headers,
          loadCandidateReviews:() => {},
          loadStats:() => {},
          loadCandidateReviewDashboard:() => {}
        };
        method.call(context);
        setImmediate(() => {
          assert.strictEqual(requests.length, 1);
          const body = JSON.parse(requests[0].options.body);
          assert.deepStrictEqual(body.ingestion_ids, ['ing-1']);
          assert.strictEqual(body.apply_model_revisions, true);
          assert.strictEqual(body.submit_ready, true);
          assert(alerts[0].includes('送审成功 1 条'));
          assert.strictEqual(context.candidateReviews.modelAnnotating, false);
        });
    """
    completed = subprocess.run(
        [node, "-e", script],
        input=FRONTEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_model_annotation_failure_is_visible_without_hiding_previous_review():
    assert "本次模型标注失败，已保留原有初标" in FRONTEND
    assert "candidateReviews.current.model_annotation.status==='failed'" in FRONTEND
    assert "candidateReviews.current.model_review.decision" in FRONTEND


def test_candidate_review_uses_draft_disposition_queue_and_update_date_filters() -> None:
    assert "草稿修订、补充证据和送审路径由模型初标与完整性门禁驱动" in FRONTEND
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


def test_annotation_failure_refreshes_saved_reply_or_blocks_stale_save() -> None:
    node = shutil.which("node")
    assert node
    script = r"""
        const assert = require('assert');
        const vm = require('vm');
        const source = require('fs').readFileSync(0, 'utf8');
        async function check(refreshOk) {
          const requests = [], notices = [];
          const sandbox = {
            API:'',
            fetch:(url, options) => {
              requests.push({url,options});
              const isPost = options.method === 'POST';
              return Promise.resolve({
                ok:!isPost && refreshOk,
                json:() => Promise.resolve(isPost ? {detail:'初标超时'} : {
                  id:'candidate-1',recommended_reply:'已根据摘要补充',draft_version:2,
                  model_annotation:{status:'failed',reason:'初标超时'}
                })
              });
            }
          };
          function method(name,next) {
            const start=source.indexOf('    '+name+': function(');
            const end=source.indexOf('    '+next+': function(',start);
            return vm.runInNewContext('('+source.slice(start,end).trim().slice(name.length+2).replace(/,$/,'')+')',sandbox);
          }
          const context = {
            candidateReviews:{current:{id:'candidate-1',recommended_reply:''},form:{recommended_reply:''},refreshRequired:{}},
            reviewReadonly:() => false, candidateDraftComplete:() => false,
            candidateCanCompleteReply:() => true, authHeaders:() => ({}),
            openCandidateReview:data => {
              context.candidateReviews.current=data;
              context.candidateReviews.form.recommended_reply=data.recommended_reply;
            },
            loadCandidateReviews:() => Promise.resolve(),
            loadCandidateReviewDashboard:() => {},
            showCandidateReviewNotice:(message,type) => notices.push({message,type})
          };
          method('annotateCandidateReviewDraft','reviewModelNeedsRevision').call(context);
          await new Promise(setImmediate);
          assert.strictEqual(requests.length,2,'初标失败后必须读取已持久化详情');
          assert.strictEqual(requests[1].url,'/integration/candidate-reviews/candidate-1');
          assert.strictEqual(context.candidateReviews.modelAnnotating,false);
          assert(notices.some(notice => notice.message.includes('初标超时')));
          if (refreshOk) {
            assert.strictEqual(context.candidateReviews.form.recommended_reply,'已根据摘要补充');
            assert.strictEqual(context.candidateReviews.current.draft_version,2);
            assert.strictEqual(context.candidateReviews.current.model_annotation.status,'failed');
            assert(!context.candidateReviews.refreshRequired['candidate-1']);
          } else {
            method('saveCandidateReview','submitCandidateReviewIds').call(context,false,false);
            assert.strictEqual(requests.length,2,'刷新失败时不能用旧空值保存');
            assert(context.candidateReviews.refreshRequired['candidate-1']);
            sandbox.fetch=(url,options) => {
              requests.push({url,options});
              return Promise.resolve({ok:true,json:() => Promise.resolve({id:'candidate-1',recommended_reply:'重新读取后的回复'})});
            };
            method('openCandidateReview','candidateReviewMediaUrl').call(context,{id:'candidate-1',recommended_reply:''});
            await new Promise(setImmediate);
            assert.strictEqual(requests[2].options.method,'GET');
            assert.strictEqual(context.candidateReviews.form.recommended_reply,'重新读取后的回复');
            assert(!context.candidateReviews.refreshRequired['candidate-1']);
          }
        }
        check(true).then(() => check(false)).catch(error => {console.error(error);process.exit(1);});
    """
    completed = subprocess.run(
        [node, "-e", script], input=FRONTEND,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_candidate_review_uses_one_human_decision_for_legacy_gate_fields() -> None:
    assert "只需选择一次复核结论" in FRONTEND
    assert "复核说明（选填）" in FRONTEND
    assert "知识草稿处理" in FRONTEND
    assert "reviewModelDraftDispositionLabel" in FRONTEND
    assert "草稿处理由模型初标决定；人工仅确认是否值得沉淀。" in FRONTEND
    assert 'v-model="candidateReviews.form.draft_disposition"' not in FRONTEND
    assert "form.draft_disposition" not in FRONTEND
    assert "draft_disposition: form." not in FRONTEND
    assert '<label class="fl">是否可用</label>' not in FRONTEND
    assert '<label class="fl">人工审核结论</label>' not in FRONTEND


def test_candidate_review_displays_shared_state_and_separates_priority_from_human_queue() -> None:
    assert "candidateReviews.summary.human_pending" in FRONTEND
    assert "candidateReviews.summary.model_priority" in FRONTEND
    assert "人工待复核" in FRONTEND
    assert "模型重点复核" in FRONTEND
    assert "candidateReviews.current.draft_version" in FRONTEND
    assert "item.blocking_reasons" in FRONTEND
    assert "candidateReviews.current.next_action" in FRONTEND
    assert "item.eligible === true" in FRONTEND
    assert "'hold_for_evidence'" in FRONTEND
    assert "'draft_incomplete'" in FRONTEND


def test_model_annotation_ui_keeps_source_evidence_optional() -> None:
    assert "annotateCandidateReviewDraft" in FRONTEND
    assert "/model-annotate" in FRONTEND
    assert "candidateReviews.form.evidence_excerpt" in FRONTEND
    assert "推荐回复为空时，模型初标会根据已保存的来源证据摘要补充" in FRONTEND
    assert "缺少摘要不会阻断保存或送审" in FRONTEND
    assert "请补充可回溯的来源证据摘要" not in FRONTEND
    assert "evidence_excerpt: form.evidence_excerpt" in FRONTEND


def test_model_annotation_button_follows_the_candidate_next_action() -> None:
    assert 'v-if="candidateCanAnnotateModel(candidateReviews.current)"' in FRONTEND
    assert "candidateCanAnnotateModel: function(item)" in FRONTEND
    assert "item.next_action === 'annotate_model'" in FRONTEND
    assert "!Object.keys(item.model_review || {}).length" in FRONTEND


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


def test_initial_annotation_can_complete_reply_only_with_saved_evidence_and_unlocked_draft() -> None:
    node = shutil.which("node")
    assert node
    assert "生成缺失的推荐回复" not in FRONTEND
    script = r"""
        const assert = require('assert');
        const vm = require('vm');
        const source = require('fs').readFileSync(0, 'utf8');
        const match = source.match(/candidateCanCompleteReply: (function\(item\) \{[\s\S]*?\n    \}),/);
        assert(match);
        const canRepair = vm.runInNewContext('(' + match[1] + ')');
        const context = {contentPreview: function(content) {
          return typeof content === 'string' && content.trim() ? content : '暂无正文';
        }};
        const base = {id:'candidate-1',title:'草稿',content:'正文',recommended_reply:'',evidence_excerpt:'来源明确的答复'};
        assert.strictEqual(canRepair.call(context, base), true);
        assert.strictEqual(canRepair.call(context, null), false);
        [
          {title:''},{content:''},{recommended_reply:'已有回复'},{evidence_excerpt:' '},
          {knowledge_id:'knowledge-1'},{submitted_at:'2026-10-25'},
          {review_status:'submitted'},{status:'review_submitted'},{status:'review_duplicate'}
        ].forEach(function(change) {
          assert.strictEqual(canRepair.call(context, Object.assign({}, base, change)), false);
        });
    """
    completed = subprocess.run(
        [node, "-e", script], input=FRONTEND,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_incomplete_worthy_draft_can_save_evidence_then_request_reply_without_submitting() -> None:
    node = shutil.which("node")
    assert node
    script = r"""
        const assert = require('assert');
        const vm = require('vm');
        const source = require('fs').readFileSync(0, 'utf8');
        const requests = [], alerts = [];
        let opened = null;
        const sandbox = {
          API:'', alert: message => alerts.push(message),
          fetch: (url, options) => {
            requests.push({url, options});
            return Promise.resolve({ok:true,json:() => Promise.resolve(
              options.method === 'PATCH'
                ? {id:'candidate-1',title:'标题',content:'正文',evidence_excerpt:'补充的可回溯来源',recommended_reply:'',eligible:false}
                : {id:'candidate-1',review_status:'pending',recommended_reply:'根据摘要补充的回复',eligible:false}
            )});
          }
        };
        function method(name, next) {
          const start = source.indexOf('    ' + name + ': function(');
          assert(start > 0);
          const end = source.indexOf('    ' + next + ': function(', start);
          const expression = source.slice(start, end).trim().slice(name.length + 2).replace(/,$/, '');
          return vm.runInNewContext('(' + expression + ')', sandbox);
        }
        const save = method('saveCandidateReview', 'submitCandidateReviewIds');
        const annotate = method('annotateCandidateReviewDraft', 'reviewModelNeedsRevision');
        const context = {
          candidateReviews:{
            current:{id:'candidate-1',recommended_reply:''},
            form:{
              title:'标题',contentText:'正文',recommended_reply:'',
              evidence_excerpt:'补充的可回溯来源',knowledge_value:'worthy',
              knowledge_origin:'business_accumulation',business_type:'self_operated',
              category_id:'category-1',originalContent:'正文',mediaBlocks:[]
            }
          },
          reviewReadonly:() => false,
          authHeaders:() => ({}),
          candidateReviewContent:text => text,
          loadCandidateReviewDashboard:() => {},
          loadCandidateReviews:() => Promise.resolve(),
          showCandidateReviewNotice:() => {},
          candidateDraftComplete:() => false,
          candidateCanCompleteReply:() => true,
          submitCandidateReviewIds:() => assert.fail('Incomplete draft must not submit'),
          openCandidateReview:data => {opened=data;}
        };
        (async function() {
          save.call(context, false, false);
          await new Promise(setImmediate);
          assert.strictEqual(alerts.length, 0);
          assert.strictEqual(requests.length, 1);
          assert.strictEqual(requests[0].options.method, 'PATCH');
          const payload = JSON.parse(requests[0].options.body);
          assert.strictEqual(payload.evidence_excerpt, '补充的可回溯来源');
          assert.strictEqual(payload.knowledge_value, 'worthy');
          assert.strictEqual(payload.recommended_reply, '');
          assert.strictEqual(context.candidateReviews.saving, false);
          annotate.call(context);
          await new Promise(setImmediate);
          assert.strictEqual(requests[1].url, '/integration/candidate-reviews/candidate-1/model-annotate');
          assert.strictEqual(opened.recommended_reply, '根据摘要补充的回复');
          assert.strictEqual(context.candidateReviews.modelAnnotating, false);
          save.call(context, true, false);
          assert.strictEqual(requests.length, 2);
          assert(alerts.includes('请填写推荐回复'));
        })().catch(error => {console.error(error);process.exit(1);});
    """
    completed = subprocess.run(
        [node, "-e", script], input=FRONTEND,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
