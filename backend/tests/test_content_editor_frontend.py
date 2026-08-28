from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_content_editor_uses_flat_stable_blocks() -> None:
    assert 'v-for="(b,bi) in fm.blocks" :key="b._uid"' in FRONTEND
    assert "createTextEditorBlock: function(value)" in FRONTEND
    assert "createMediaEditorBlock: function(type, source)" in FRONTEND
    assert "var content = {blocks:self.buildContentBlocks(form)};" in FRONTEND
    assert "b.children" not in FRONTEND
    assert "block.children" not in FRONTEND


def test_media_decorators_are_only_trimmed_next_to_media() -> None:
    assert "isMediaDecorationLine: function(value)" in FRONTEND
    assert (
        "return /^[+\\-‐‑‒–—―*•·●○▪▫]$/.test"
        in FRONTEND
    )
    assert "trimMediaPlaceholderLines: function(" in FRONTEND
    assert "normalizeAdjacentMediaDecorators: function(blocks)" in FRONTEND
    assert "appendText(text.slice(cursor, offset), foundMedia, true);" in FRONTEND
    assert "appendText(text.slice(cursor), foundMedia, false);" in FRONTEND


def test_media_lifecycle_scans_flat_blocks_and_releases_blob_urls() -> None:
    assert 'class="upload-zone" :data-block-uid="b._uid"' in FRONTEND
    assert "operation.uploads.push({" in FRONTEND
    assert "block: block," in FRONTEND
    assert "revokeContentBlockPreview: function(block)" in FRONTEND
    assert "URL.revokeObjectURL(block.preview)" in FRONTEND
    assert "releaseContentPreviews(this.fm.blocks);" in FRONTEND


def test_content_editor_has_one_hint_and_visible_media_field_labels() -> None:
    assert FRONTEND.count("插件媒体标记使用") == 1
    assert "{{b.type==='image'?'图片':'视频'}}标题" in FRONTEND
    assert "{{b.type==='image'?'图片':'视频'}}说明" in FRONTEND
    assert "📷" not in FRONTEND
    assert "🎬" not in FRONTEND


def test_knowledge_editor_exposes_and_persists_recommended_reply() -> None:
    assert 'v-model="fm.recommendedReply"' in FRONTEND
    assert 'placeholder="供答疑人员直接使用的推荐回复"' in FRONTEND
    assert "recommendedReply: String(recommendedReply || '')" in FRONTEND
    assert "content.recommended_reply = String(form.recommendedReply || '').trim();" in FRONTEND


def test_view_mode_media_uses_thumbnail_as_the_only_preview_entry() -> None:
    start = FRONTEND.index('<figure v-else class="content-article-media">')
    end = FRONTEND.index("</figure>", start)
    view_markup = FRONTEND[start:end]

    assert view_markup.count('@click="showFull(') == 1
    assert "content-media-preview-overlay" in view_markup
    assert "打开外链" in view_markup
