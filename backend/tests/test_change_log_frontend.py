from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_change_log_renders_grouped_field_level_before_and_after_values() -> None:
    assert 'v-for="group in changeLogGroups(log)"' in FRONTEND
    assert "修改{{change.label}}" in FRONTEND
    assert "{{change.before_text}}" in FRONTEND
    assert "{{change.after_text}}" in FRONTEND
    assert "变更前" in FRONTEND
    assert "变更后" in FRONTEND
    assert "changeFieldSection: function(field)" in FRONTEND
    assert "changeLogValue: function(field, value, hasValue, isAfter)" in FRONTEND
    assert "changeLogGroups: function(log)" in FRONTEND


def test_change_log_formats_content_and_status_without_exposing_source_fields() -> None:
    assert "changeLogContentValue: function(content)" in FRONTEND
    assert "if (field === 'status') return this.sl(value);" in FRONTEND
    assert "if (field === 'content') return this.changeLogContentValue(value);" in FRONTEND
    assert "内部来源字段已更新，具体值不在前端展示" in FRONTEND
    assert (
        'v-if="r.status===\'published\' || r.status===\'deprecated\'"'
        in FRONTEND
    )
    assert (
        '<span class="ti" v-for="field in log.changed_fields"'
        not in FRONTEND
    )
