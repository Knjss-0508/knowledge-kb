from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_candidate_review_has_fast_batch_annotation_and_update_date_filters() -> None:
    assert "candidate-reviews:batch-annotate" in FRONTEND
    assert "批量标为不沉淀" in FRONTEND
    assert "quickAnnotateCandidateReview(item,'unworthy')" in FRONTEND
    assert "params.set('updated_from', filter.updatedFrom)" in FRONTEND
    assert "params.set('updated_to', filter.updatedTo)" in FRONTEND


def test_candidate_review_uses_one_human_decision_for_legacy_gate_fields() -> None:
    assert "只需选择一次复核结论" in FRONTEND
    assert "复核说明（选填）" in FRONTEND
    assert "var usability = form.knowledge_value === 'worthy'" in FRONTEND
    assert "var decision = form.knowledge_value === 'worthy'" in FRONTEND
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
