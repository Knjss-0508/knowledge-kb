from types import SimpleNamespace

import pytest

from answer_hub import workflow as w


def test_unanswered_atomic_question_never_calls_transcription(monkeypatch):
    rows = [{
        "数据ID": "unanswered", "产品类型": "笔记本",
        "_原子知识ID": "unanswered-U1", "_原子来源问题数": 2,
        "_原子问答配对状态": "paired",
        "_原子参考话术": "请使用工具读取内存品牌后填写。",
        "核心问题": "内存是否为品牌", "对象/部位": "内存",
        "判定结论": "建议使用工具读取内存品牌。",
        "聊天内容": "回收师：内存是不是品牌？\n回收师：没有指纹选项\n答疑：不支持指纹",
    }]
    calls = []
    client = SimpleNamespace(
        config=SimpleNamespace(model="test"),
        label_topic=lambda *a, **k: calls.append("label"),
        classify_topic_stage=lambda *a, **k: calls.append("stage"),
    )
    monkeypatch.setattr(w, "_direct_mimo_topic_groups", lambda *a, **k: (
        [(("frozen",), rows)], {"effective_mode": "direct_mimo"},
    ))
    topics, mappings, gaps, _ = w.build_topic_review_rows(
        rows, mimo_client=client, use_mimo=False, clustering_mode="direct_mimo",
        use_standard_references=False, enforce_cluster_admission=False,
    )
    assert calls == []
    assert not any(t.get("知识内容") or t.get("推荐回复") for t in topics)
    assert gaps and "人工" in gaps[0]["证据缺口原因"]


def test_source_without_answer_is_excluded_before_clustering(monkeypatch):
    def unexpected_cluster_call(*args, **kwargs):
        pytest.fail("A source with no human answer must not invoke clustering")

    monkeypatch.setattr(w, "_direct_mimo_topic_groups", unexpected_cluster_call)
    topics, mappings, gaps, _ = w.build_topic_review_rows(
        [{"数据ID": "waiting", "产品类型": "手机", "核心问题": "电池异常怎么判",
          "聊天内容": "回收师：电池异常怎么判\n答疑：稍等",
          "参考话术": "建议勾选电池异常。"}],
        mimo_client=SimpleNamespace(config=SimpleNamespace(model="test")),
        use_mimo=False, clustering_mode="direct_mimo", use_standard_references=False,
    )
    assert not topics and not mappings
    assert len(gaps) == 1


def test_standard_miss_transcribes_clear_human_answer_without_case_audit_template():
    candidate = w._topic_rule_draft(
        "TOP-COLOR-SOURCE-RULE",
        [{
            "数据ID": "color-source",
            "工单ID": "color-work",
            "_人工答复门禁状态": "answered",
            "_原子知识ID": "color-source-U1",
            "产品类型": "手机",
            "核心问题": "整机颜色项如何判定",
            "对象/部位": "整机颜色项",
            "异常现象": "系统拉取颜色与实物颜色不一致",
            "_原子实际回复": (
                "颜色一律以实物为准录入，系统拉取的颜色仅作参考，"
                "实物偏黄就按实物颜色判定。"
            ),
            "聊天内容": (
                "回收师反馈质检录入系统中拉取显示的颜色与实物颜色不一致，"
                "询问颜色项应如何判定。\n"
                "答疑：颜色一律以实物为准录入，系统拉取的颜色仅作参考。"
            ),
        }],
        [],
        use_standard_references=True,
    )

    assert "颜色一律以实物为准录入" in candidate["content"]
    assert "系统拉取的颜色仅作参考" in candidate["content"]
    assert "回收师反馈" not in candidate["content"]
    assert "询问" not in candidate["content"]
    assert "截图" not in candidate["content"]
    assert "关键事实" not in candidate["content"]
    assert "仅覆盖上述来源事实" not in candidate["content"]
    assert "补充对应事实后再判断" not in candidate["content"]


def test_standard_miss_can_turn_a_short_human_judgment_into_a_scoped_rule():
    candidate = w._topic_rule_draft(
        "TOP-FOLDING-SOURCE-RULE",
        [{
            "数据ID": "folding-source",
            "工单ID": "folding-work",
            "_人工答复门禁状态": "answered",
            "_原子知识ID": "folding-source-U1",
            "产品类型": "手机",
            "核心问题": "折叠屏手机折叠后关机如何判定",
            "对象/部位": "开机情况",
            "异常现象": "折叠后关机",
            "_原子实际回复": "判自动关机",
            "聊天内容": (
                "回收师：折叠屏手机折叠后关机如何判定\n"
                "答疑：判自动关机"
            ),
        }],
        [],
        use_standard_references=True,
    )

    assert "折叠后关机" in candidate["content"]
    assert "自动关机" in candidate["content"]
    assert "核验对象：" not in candidate["content"]
    assert "核验方法：" not in candidate["content"]
    assert "处理边界：" not in candidate["content"]


def test_reference_reply_can_enrich_answered_topic_without_becoming_human_fact():
    row = {
        "数据ID": "reference-enrichment",
        "产品类型": "手机",
        "_人工答复门禁状态": "answered",
        "_原子知识ID": "reference-enrichment-U1",
        "_原子实际回复": "无法测量建议邮寄回收",
        "核心问题": "屏幕无触感划痕如何按轻项判定",
        "对象/部位": "屏幕无触感划痕",
        "异常现象": "强光下大量细小划痕且数量无法估量",
        "参考话术": (
            "无触感划痕需按长度和数量判定；现场无法准确测量时，"
            "建议邮寄回收，由质检中心判定。"
        ),
        "聊天内容": (
            "回收师：强光下有大量细小无触感划痕，数量无法估量\n"
            "答疑：无法测量建议邮寄回收"
        ),
    }

    assert "长度和数量判定" in w._topic_reference_reply(row)
    query = w._topic_query([row])
    assert "长度和数量判定" in query["参考话术"]
    selected = w._select_topic_recommended_reply(
        [row],
        title="屏幕无触感划痕如何按轻项判定",
        content="1. 屏幕无触感划痕无法准确测量时，建议邮寄回收。",
        model_reply="",
        evidence_status="available",
        use_standard_references=False,
    )
    assert selected["source_type"] == "second_part_reference_reply"
    assert "长度和数量判定" in selected["reply"]


def test_reference_reply_does_not_create_content_without_verified_answer():
    query = {
        "产品类型": "手机",
        "对象/部位": "电池",
        "异常现象": "读取异常",
        "参考话术": "建议勾选电池异常。",
        "人工判定结论": "",
        "历史实际回复": "",
    }
    assert w._build_experience_review_content(query) == ""


def test_model_empty_content_claim_is_replaced_when_draft_fields_are_present():
    topic = {
        "主标题": "AirPods 四代是否具备查找功能",
        "知识内容": "1. Apple AirPods 四代没有查找定位功能。",
        "推荐回复": "Apple AirPods 四代没有查找定位功能。",
        "主题证据等级": "完整会话",
        "正文类型": "定义型",
        "关联标准项": "耳机-定位查找功能",
        "主题标准检索来源": "local_quality_standard",
    }
    guarded = w._apply_topic_initial_review_guard(
        {
            "decision": "需修改",
            "knowledge_value": "不值得沉淀",
            "error_type": "正文与推荐回复为空",
            "reason": "草稿正文与推荐回复均为空，无法审核。",
            "standard_consistency": "一致",
            "content_consistency": "不一致",
            "priority_review": True,
        },
        topic,
        [],
        use_standard_references=False,
    )
    assert guarded["error_type"] == "模型审核字段误判"
    assert "候选字段均有内容" in guarded["reason"]
    assert "草稿正文与推荐回复均为空" not in guarded["reason"]
    assert guarded["knowledge_value"] != "不值得沉淀"


def test_model_empty_content_claim_with_jun_empty_marker_is_replaced():
    topic = {
        "主标题": "AirPods 四代是否具备查找功能",
        "知识内容": "1. Apple AirPods 四代没有查找定位功能。",
        "推荐回复": "Apple AirPods 四代没有查找定位功能。",
        "主题证据等级": "完整会话",
        "正文类型": "定义型",
        "关联标准项": "",
        "主题标准检索来源": "",
    }
    guarded = w._apply_topic_initial_review_guard(
        {
            "decision": "需修改",
            "knowledge_value": "不值得沉淀",
            "error_type": "正文与推荐回复均为空",
            "reason": "正文与推荐回复均为空；无正文支撑。",
            "standard_consistency": "无可信标准",
            "content_consistency": "不一致",
            "priority_review": True,
        },
        topic,
        [],
        use_standard_references=False,
    )

    assert guarded["error_type"] == "模型审核字段误判"
    assert "候选字段均有内容" in guarded["reason"]
    assert guarded["knowledge_value"] == "值得沉淀"


def test_obvious_business_typo_is_repaired_but_uncertain_text_is_flagged():
    repaired, repairs = w._repair_obvious_source_typos(
        "外科岁；碎。",
        source_context="手机素皮磨平，按外壳碎裂处理。",
    )

    assert repaired == "外壳碎；碎。"
    assert repairs
    assert "疑似错别字" not in w._text_quality_issues(repaired)


def test_uncertain_text_is_not_guessed():
    assert "疑似错别字" in w._text_quality_issues("外科岁。")


@pytest.mark.parametrize("actual,reference,expected", [
    (
        "多次恢复 没这个才能收 如果还是有 不收",
        "本地退ID不等于云端解绑，仍显示已为拥有者锁定iPhone时不能回收；"
        "请机主关闭查找并退出账号后抹除恢复，恢复后仍有锁定提示则不回收。",
        "second_part_reference_reply",
    ),
    (
        "按外科碎",
        "素皮表面磨平属于外壳磨损，不能按外壳碎裂处理。",
        "knowledge_content_generation",
    ),
])
def test_standard_rejection_reselects_reply_without_losing_reference_or_reintroducing_typos(
    actual, reference, expected,
):
    import json

    topic = {
        "主标题": "手机账号锁定如何处理" if "恢复" in actual else "手机素皮后盖如何判定",
        "主题对象/部位": "账号状态" if "恢复" in actual else "外壳",
        "主题异常现象": "已为拥有者锁定iPhone" if "恢复" in actual else "素皮磨平",
        "关联标准项": "QC-WRONG", "标准引用标签": "已引用标准知识点",
        "模型初标错误类型": "标准项映射错", "模型初标原因": "标准项映射错",
        "知识内容": "旧标准正文。", "推荐回复": reference,
        "主题事实证据包": json.dumps({"representative_facts": [{
            "source_record_id": "ref-source", "historical_actual_reply": actual,
            "human_judgment_conclusion": actual, "reference_reply": reference,
            "atomic_question": "手机账号锁定如何处理" if "恢复" in actual else "素皮后盖如何判定",
            "object": "账号状态" if "恢复" in actual else "外壳",
        }]}, ensure_ascii=False),
    }
    final = w._enforce_standard_reference_consistency(topic, use_standard_references=True)
    assert final["推荐回复来源类型"] == expected
    assert final["推荐回复改写文本"] == final["推荐回复"]
    assert "外科" not in final["知识内容"] + final["推荐回复"]
    if expected == "second_part_reference_reply":
        assert "关闭查找" in final["推荐回复"]
    else:
        assert "外壳碎" in final["推荐回复"]
        assert "属于外壳磨损" not in final["推荐回复"]
    assert topic["主题事实证据包"] == final["主题事实证据包"]


def test_final_typo_repair_covers_standard_rebuild_and_preserves_source():
    topic = {
        "主标题": "无触感划痕按长度和数量如何判定",
        "知识内容": "1. 按数量尺寸盘。\n2. 无法测量建议邮寄回收。",
        "推荐回复": "按数量尺寸盘，无法测量建议邮寄回收。",
        "推荐回复原文": "按数量尺寸盘\n无法测量建议邮寄回收",
    }
    repaired = w._enforce_standard_reference_consistency(topic, use_standard_references=False)
    assert "按数量、尺寸判定" in repaired["知识内容"]
    assert repaired["推荐回复原文"] == topic["推荐回复原文"]
    assert repaired["推荐回复改写文本"] == repaired["推荐回复"]


def test_four_supported_threshold_points_do_not_fail_due_to_style_count():
    from answer_hub.mimo import _content_type_has_expected_numbered_points

    assert _content_type_has_expected_numbered_points(
        "1. 明确对象。\n2. 测量长度。\n3. 统计数量。\n4. 无法测量时邮寄回收。",
        "阈值型",
    )


def test_one_sentence_model_fact_survives_standard_miss():
    row = {
        "数据ID": "model-fact", "_原子知识ID": "model-fact-U1",
        "_人工答复门禁状态": "answered",
        "_原子实际回复": "Apple AirPods 四代 这一款没有定位的",
        "产品类型": "耳机/耳麦", "核心问题": "AirPods四代是否具备定位功能",
        "对象/部位": "定位功能", "异常现象": "机型定位功能查询",
        "聊天内容": "回收师：AirPods四代是否具备定位功能\n答疑：Apple AirPods 四代 这一款没有定位的",
    }
    candidate = {
        "title": "AirPods四代是否具备定位功能", "subtitles": [],
        "content": "1. Apple AirPods 四代没有定位功能。",
        "recommended_reply": "Apple AirPods 四代没有定位功能。",
        "knowledge_form": "具体判定", "content_type": "定义型",
        "confidence": 0.9, "standard_refs": [], "applicable_scope": "耳机/耳麦",
    }
    topic = w._topic_candidate_row(
        "fact", ("fact",), [row], [], candidate, "mimo", "test", "v1", "run",
        "", "topic_model_labeled", 0.8, use_standard_references=True,
    )
    assert "AirPods" in topic["知识内容"] and "没有定位" in topic["知识内容"]
    assert "没有定位" in topic["推荐回复"]
    assert "正文留空" not in topic["校验备注"]


def test_initial_review_is_repeated_after_final_draft_rebuild(monkeypatch, tmp_path):
    import json
    import sqlite3

    from answer_hub.audit import AuditStore
    from answer_hub.mimo import MimoLabelResult

    reviewed = []
    original_enforce = w._enforce_standard_reference_consistency
    original_body = "折叠屏手机在折叠后关机时，按自动关机判定。"
    final_body = "折叠屏手机折叠时自动关机，按自动关机处理。"

    def enforce(topic, **kwargs):
        topic = original_enforce(topic, **kwargs)
        if topic.get("模型初标结论") and topic.get("知识内容") != final_body:
            topic["知识内容"] = final_body
            topic["推荐回复"] = final_body
        return topic

    def review(payload, draft, matches, **kwargs):
        reviewed.append(dict(draft))
        return MimoLabelResult(candidate={
            "decision": "通过", "knowledge_value": "值得沉淀",
            "reason": "最终正文有明确的处理结论。", "error_type": "",
            "standard_consistency": "无可信标准", "content_consistency": "一致",
            "evidence_sufficiency": "充分", "confidence": 0.95,
            "priority_review": False,
        }, request_audit={"draft": draft}, response_audit={"review": len(reviewed)})

    monkeypatch.setattr(w, "_enforce_standard_reference_consistency", enforce)
    store = AuditStore(tmp_path / "audit.db")
    topics, _, _, _ = w.build_topic_review_rows([{
        "数据ID": "rebuilt", "_原子知识ID": "rebuilt-U1",
        "产品类型": "手机", "核心问题": "折叠屏手机折叠后关机如何判定",
        "对象/部位": "开机情况", "异常现象": "折叠后关机",
        "聊天内容": "回收师：折叠屏手机折叠后关机如何判定\n答疑：判自动关机",
    }], mimo_client=SimpleNamespace(
        config=SimpleNamespace(model="test-final-review"),
        label_topic=lambda *a, **k: MimoLabelResult(candidate={
            "title": "折叠屏手机折叠后关机如何判定", "subtitles": [],
            "content": original_body, "recommended_reply": original_body,
            "knowledge_form": "具体判定", "content_type": "定义型",
            "confidence": 0.95, "standard_refs": [], "applicable_scope": "手机",
        }, request_audit={}, response_audit={}),
        review_topic=review,
    ), use_mimo=True, clustering_mode="rule", use_standard_references=False,
        transcribe_all_admitted_topics=True, audit_store=store, run_id="rebuild")
    assert len(reviewed) == 2
    assert reviewed[0]["知识内容"] != final_body
    assert reviewed[1]["知识内容"] == reviewed[1]["推荐回复"] == final_body
    assert topics[0]["知识内容"] == final_body
    assert topics[0]["模型初标状态"] == "topic_initial_reviewed_model_after_rebuild"
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        row = conn.execute(
            "SELECT provider, model_name, request_json, response_json FROM model_runs "
            "WHERE status = 'topic_initial_reviewed_model_after_rebuild'"
        ).fetchone()
    assert row[:2] == ("mimo", "test-final-review")
    assert json.loads(row[2])["final_draft"]["知识内容"] == final_body
    assert json.loads(row[3])["after_rebuild"]["review"] == 2


def test_selected_standard_controls_body_and_handling_instead_of_top_search_hit():
    from answer_hub.catalog import StandardCatalogItem

    def standard(code, title, body):
        return StandardCatalogItem(
            standard_id=code, title=title, category_l1="基本情况", category_l2="账号状态",
            knowledge_type="质检标准", standard_path=f"【账号状态】-【{title}】",
            keywords=["账号"], scope="手机", response_snippet=body,
            status="published", version="v1",
        )

    wrong = standard("QC-WRONG", "账号已注销", "本地账号和云端账号均已关闭。")
    correct = standard("QC-RIGHT", "账号无法注销", "本地或云端账号有其一无法注销，即按账号无法注销处理。")
    topic = w._topic_candidate_row(
        "selected", ("selected",), [{
            "数据ID": "selected", "_人工答复门禁状态": "answered",
            "_原子实际回复": "云端账号无法注销，不收。",
            "产品类型": "手机", "核心问题": "云端账号无法注销如何判定",
            "对象/部位": "账号状态", "异常现象": "云端账号无法注销",
            "聊天内容": "回收师：云端账号无法注销如何判定\n答疑：云端账号无法注销，不收。",
        }], [(wrong, 10), (correct, 9)], {
            "title": "云端账号无法注销如何判定", "content": correct.response_snippet,
            "standard_refs": ["QC-RIGHT"], "content_type": "定义型",
            "knowledge_form": "具体判定", "confidence": 0.9,
        }, "mimo", "test", "v1", "run", "", "topic_model_labeled", 0.8,
    )
    assert "本地账号和云端账号均已关闭" not in topic["知识内容"]
    assert "账号无法注销" in topic["知识内容"]
    assert "账号已注销" not in topic["候选项/处理项"]


def test_standard_miss_with_no_human_answer_stays_empty():
    candidate = w._topic_rule_draft(
        "TOP-NO-ANSWER",
        [{
            "数据ID": "no-answer",
            "工单ID": "no-answer-work",
            "_原子知识ID": "no-answer-U1",
            "产品类型": "手机",
            "核心问题": "电池异常如何勾选",
            "对象/部位": "电池",
            "异常现象": "读取异常",
            "聊天内容": "回收师：电池异常如何勾选\n答疑：稍等，我确认一下。",
            "历史实际回复": "",
            "人工判定结论": "",
        }],
        [],
        use_standard_references=True,
    )

    assert candidate["content"] == ""
    assert candidate["source_answer_missing"] is True


def test_measurement_gap_does_not_replace_source_rule_with_generic_reply():
    reply = w._recommended_reply_from_final_content(
        "1. 断开痕迹分别测量，相连痕迹累计；超过10mm选最大档。",
        evidence_status="required_missing",
    )
    assert "分别测量" in reply and "累计" in reply and "10mm" in reply


def test_measurement_gap_stays_in_audit_instead_of_business_body():
    content = "1. 划痕断开时分别测量，相连时累计；超过10mm选最大档。"
    topic = w._topic_candidate_row(
        "measurement", ("measurement",), [{
            "数据ID": "measurement", "产品类型": "手机", "对象/部位": "外壳",
            "核心问题": "外壳划痕如何测量", "图片链接": "https://example.com/fixture.jpg",
            "聊天内容": "回收师：外壳划痕如何测量\n答疑：划痕断开时分别测量，相连时累计；超过10mm选最大档。",
            "实际回复": "划痕断开时分别测量，相连时累计；超过10mm选最大档。",
        }], [], {
            "title": "外壳划痕如何测量", "content": content,
            "recommended_reply": "划痕断开时分别测量，相连时累计；超过10mm选最大档。",
            "knowledge_form": "具体判定", "content_type": "阈值型",
            "confidence": 0.95, "subtitles": [], "standard_refs": [],
            "applicable_scope": "手机", "applicable_brands": [], "applicable_models": [],
        }, "test", "test", "test", "test", "", "topic_model_labeled", 0.75,
        use_standard_references=False,
    )
    assert "当前案例图片" not in topic["知识内容"]
    assert "分别测量" in topic["知识内容"] and "累计" in topic["知识内容"]
    assert topic["图片证据门禁状态"] == "required_missing"
    assert topic["知识草稿状态"] == "blocked"


def test_semantic_binding_allows_component_split_with_reordered_answers():
    bound, status, _ = w._bind_ordered_question_reply_pairs(
        {"核心问题": "内存和硬盘是否品牌", "参考话术": "硬盘是三星；内存是海力士"},
        [{"normalized_issue": "内存品牌", "subject": "内存"},
         {"normalized_issue": "硬盘品牌", "subject": "硬盘"}],
    )
    assert status == "semantic_paired"
    assert bound[0]["_source_reference_reply"] == "内存是海力士"
    assert bound[1]["_source_reference_reply"] == "硬盘是三星"


def test_verified_payload_does_not_reintroduce_generated_reference_conditions():
    import json

    row = {
        "数据ID": "folding", "_原子知识ID": "folding-U1",
        "_人工答复门禁状态": "answered", "_原子实际回复": "判自动关机",
        "产品类型": "手机", "核心问题": "折叠屏折叠后关机如何选择",
        "对象/部位": "开机情况", "异常现象": "折叠后关机",
        "聊天内容": "回收师：一折就关机\n答疑：判自动关机",
        "原始判定结论": "电量正常，未设定时关机，出现一次及以上即可判定。",
        "参考话术": "电量正常，未设定时关机，出现一次及以上即可判定。",
        "解题方式": "确认电量正常，未设定时关机。",
        "语义标注依据": "出现一次及以上即可判定。",
        "_原子阈值例外": "出现一次及以上",
    }
    payload = w._topic_stage_payload("folding", [row], use_standard_references=False)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "判自动关机" in serialized
    assert payload["historical_replies"] == ["判自动关机"]
    assert payload["representative_human_judgments"] == ["判自动关机"]
    assert payload["reference_replies"] == [
        "电量正常，未设定时关机，出现一次及以上即可判定。"
    ]
    query = w._topic_query([row])
    assert query["解题方式"] == "判自动关机"


def test_verified_reply_candidates_exclude_unscoped_conversation_answer(monkeypatch):
    seen = []
    original = w._reply_sentence

    def capture_reply(value):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(w, "_reply_sentence", capture_reply)
    selected = w._select_topic_recommended_reply(
        [{
            "数据ID": "folding", "_人工答复门禁状态": "answered",
            "_原子实际回复": "折叠后关机按自动关机勾选。",
            "实际回复": "折叠后关机按自动关机勾选；内存品牌填写三星。",
            "产品类型": "手机", "核心问题": "折叠后关机如何勾选",
            "对象/部位": "开机情况", "异常现象": "折叠后关机",
        }],
        title="折叠后关机如何勾选",
        content="折叠后出现关机时，按自动关机勾选并记录实际现象。",
        model_reply="", evidence_status="available", use_standard_references=False,
    )
    assert selected["reply"] == "折叠后关机按自动关机勾选。"
    assert selected["source_candidates"]
    assert all("内存" not in entry["reply"] for entry in selected["source_candidates"])
    assert all("内存" not in str(value) for value in seen)


@pytest.mark.parametrize("cached_version", [None, "legacy", "current"])
@pytest.mark.parametrize("version_field", ["knowledge_value_guard_version", "topic_output_evidence_version"])
def test_resume_checks_knowledge_value_guard_version(tmp_path, monkeypatch, cached_version, version_field):
    import json

    from answer_hub.knowledge_value import KNOWLEDGE_VALUE_EVIDENCE_GUARD_VERSION
    current_version = (
        KNOWLEDGE_VALUE_EVIDENCE_GUARD_VERSION
        if version_field == "knowledge_value_guard_version"
        else w.TOPIC_OUTPUT_EVIDENCE_VERSION
    )

    source = tmp_path / "source.xlsx"
    row = {
        "工单ID": "guard-version", "产品类型": "手机",
        "核心问题": "折叠后关机如何勾选",
        "聊天内容": "回收师：折叠后关机如何勾选\n答疑：折叠后关机按自动关机勾选。",
    }
    w.write_rows_to_workbook({"source": (list(row), [row])}, source)
    options = {
        "source_path": source, "standards_path": None,
        "output_dir": tmp_path / "output", "audit_db_path": tmp_path / "audit.db",
        "use_mimo": False, "clustering_mode": "rule", "use_standard_references": False,
        "enforce_cluster_admission": False,
    }
    w.initial_label_from_workbook(**options)
    checkpoint_path = tmp_path / "output/workflow_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    summary = checkpoint["topic_summary"]
    if cached_version is None:
        summary.pop(version_field, None)
    else:
        summary[version_field] = current_version if cached_version == "current" else cached_version
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    calls = []
    original = w.build_topic_review_rows

    def capture_build(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(w, "build_topic_review_rows", capture_build)
    w.initial_label_from_workbook(**options, resume=True)
    assert len(calls) == (0 if cached_version == "current" else 1)
    refreshed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert refreshed["topic_summary"][version_field] == current_version


def test_numbered_policy_labels_are_removed_without_losing_conditions():
    body = w._template_style_knowledge_content(
        "1. 适用情形：三星折叠屏折叠后关机。\n"
        "2. 处理结论：按自动关机勾选。\n"
        "3. 边界：其他机型不得套用。"
    )
    assert "适用情形：" not in body
    assert "处理结论：" not in body
    assert "边界：" not in body
    assert "其他机型不得套用" in body
    assert "1. 1." not in body


def test_empty_body_cannot_receive_generated_reply():
    selected = w._select_topic_recommended_reply(
        [{"参考话术": "请补充屏幕照片。"}],
        title="屏幕异常如何判断", content="", model_reply="",
        evidence_status="available", use_standard_references=False,
    )
    assert selected["reply"] == ""


def test_multi_source_reply_does_not_select_first_brand_answer():
    rows = [{
        "数据ID": source, "产品类型": "笔记本", "对象/部位": "内存",
        "核心问题": "内存品牌如何识别", "参考话术": reply,
    } for source, reply in [
        ("a", "该内存品牌是三星。"), ("b", "该内存品牌是海力士。"),
    ]]
    selected = w._select_topic_recommended_reply(
        rows, title="内存品牌如何识别",
        content="1. 该内存品牌是三星。\n2. 另一来源内存品牌是海力士。",
        model_reply="来源中两种内存品牌分别为三星和海力士。",
        evidence_status="available", use_standard_references=False,
    )
    assert "三星" in selected["reply"] and "海力士" in selected["reply"]


def test_multi_source_model_reply_cannot_drop_other_member_brand():
    rows = [{
        "数据ID": source, "产品类型": "笔记本", "对象/部位": "内存",
        "_原子实际回复": reply, "_人工答复门禁状态": "answered",
    } for source, reply in [("a", "内存是三星。"), ("b", "内存是海力士。")]]
    selected = w._select_topic_recommended_reply(
        rows, title="内存品牌如何识别",
        content="1. 两条来源中的内存品牌分别是三星和海力士。",
        model_reply="内存品牌是三星。",
        evidence_status="available", use_standard_references=False,
    )
    assert "海力士" in selected["reply"]


@pytest.mark.parametrize("status", ["validation_failed", "call_failed"])
def test_failed_transcription_has_only_audit_fields(status):
    topic = w._failed_topic_transcription_row(
        "failed", ("failed",), [{"产品类型": "手机", "核心问题": "屏幕如何判断",
                                 "参考话术": "请补充图片。"}], {},
        provider="test", model_name="test", prompt_version="test",
        model_run_id="test", transcription_status=status,
        model_call_status="model_failed", error="模拟失败",
        matches=[], use_standard_references=False,
    )
    assert topic["知识内容"] == topic["推荐回复"] == ""
    assert "模拟失败" in topic["校验备注"]


def test_transcription_input_and_source_mapping_use_verified_answer_only():
    import json

    from answer_hub.mimo import MimoLabelResult

    captured = []

    def label_topic(topic, matches, **kwargs):
        captured.append(topic)
        return MimoLabelResult(
            candidate={
                "title": "折叠屏手机折叠后关机如何判定",
                "subtitles": [],
                "content": "折叠屏手机在折叠后关机时，按自动关机判定。",
                "recommended_reply": "折叠后关机按自动关机判定。",
                "knowledge_form": "具体判定",
                "content_type": "定义型",
                "confidence": 0.95,
                "standard_refs": [],
                "applicable_scope": "手机",
                "applicable_brands": [],
                "applicable_models": [],
            },
            request_audit={}, response_audit={},
        )

    _, mapping, _, _ = w.build_topic_review_rows(
        [{
            "数据ID": "folding", "产品类型": "手机",
            "_原子知识ID": "folding-U1",
            "核心问题": "折叠屏手机折叠后关机如何判定",
            "对象/部位": "开机情况", "异常现象": "折叠后关机",
            "问题意图": "标准判定", "解题方式": "确认电量正常且未设定时关机后判定",
            "主题标签": "意图:标准判定 | 对象:开机情况 | 处理:确认电量正常且未设定时关机后判定",
            "聊天内容": "回收师：折叠屏手机折叠后关机如何判定\n答疑：判自动关机",
        }],
        mimo_client=SimpleNamespace(config=SimpleNamespace(model="test"), label_topic=label_topic),
        use_mimo=True, clustering_mode="rule", use_standard_references=False,
        enforce_cluster_admission=False, transcribe_all_admitted_topics=True,
    )
    assert captured, "必须覆盖实际转写调用入口"
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "电量正常" not in serialized and "未设定时" not in serialized
    assert mapping and mapping[0]["历史实际回复"] == "判自动关机"


@pytest.mark.parametrize("actual,subject,observation,question,body", [
    (
        "3.5的", "耳机孔", "接口规格查询",
        "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
        "2019款16寸MacBook Pro的耳机孔为3.5mm规格。",
    ),
    (
        "3.5的", "耳机孔", "接口规格查询",
        "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
        "2019款16寸MacBook Pro的耳机孔为3.5mm接口，即3.5mm耳机插孔。",
    ),
    (
        "3.5的", "耳机孔", "接口规格查询",
        "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
        "2019款16寸MacBook Pro耳机孔规格为3.5毫米。",
    ),
    (
        "3.5的", "耳机孔", "接口规格查询",
        "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
        "2019款16寸MacBook Pro的耳机孔为3.5mm接口规格。",
    ),
    (
        "耳机接口功能异常(失灵/变形导致无法使用)", "耳机接口", "无法插入耳机",
        "笔记本｜耳机接口｜无法插入耳机",
        "笔记本耳机无法插入耳机接口时，按耳机接口功能异常处理。",
    ),
])
@pytest.mark.parametrize("first_generic", [False, True])
def test_explicit_single_fact_is_not_rejected_for_missing_extra_steps(
    actual, subject, observation, question, body, first_generic,
):
    from answer_hub.mimo import MimoLabelResult

    def stage(topic):
        return MimoLabelResult(candidate={
            "topic_stage": "课外常识" if subject == "耳机孔" else "质检标准",
            "knowledge_value": "值得沉淀", "stage_reason": "对象与结论明确。",
            "value_reason": "明确规格或判定选项可复用。",
            "reusable_knowledge": body, "confidence": 0.9, "needs_human_review": True,
        }, request_audit={}, response_audit={})

    retry_reasons = []

    def label(topic, matches, retry_reason="", **kwargs):
        retry_reasons.append(retry_reason)
        return MimoLabelResult(candidate={
            "title": "笔记本耳机孔如何判定", "subtitles": [],
            "content": "请补充图片。" if first_generic and len(retry_reasons) == 1 else body,
            "recommended_reply": actual,
            "knowledge_form": "具体判定", "content_type": "定义型",
            "confidence": 0.95, "standard_refs": [], "applicable_scope": "笔记本",
            "applicable_brands": [], "applicable_models": [],
        }, request_audit={}, response_audit={})

    topics, _, _, _ = w.build_topic_review_rows([{
        "数据ID": "explicit-spec", "_原子知识ID": "explicit-spec-U1",
        "产品类型": "笔记本", "核心问题": question,
        "对象/部位": subject, "异常现象": observation,
        "主题标签": "处理:确认电量正常且未设定时关机后填写",
        "聊天内容": f"回收师：{question}\n答疑：{actual}",
    }], mimo_client=SimpleNamespace(
        config=SimpleNamespace(model="test"), classify_topic_stage=stage, label_topic=label,
    ), use_mimo=True, clustering_mode="rule", use_standard_references=False,
       transcribe_all_admitted_topics=True)
    assert topics[0]["主题沉淀价值"] == "值得沉淀"
    assert "电量正常" not in topics[0]["检索关键词"]
    if first_generic:
        assert len(retry_reasons) == 2
        assert "以及不能适用时的边界" not in retry_reasons[1]
        assert "只保留来源明确提供的" in retry_reasons[1]
    if subject == "耳机孔":
        assert topics[0]["模型质量状态"] == "passed"
        assert not topics[0]["主题无来源内容"]
        assert topics[0]["推荐回复"] == body


@pytest.mark.parametrize("body", [
    "2020款16寸MacBook Pro的耳机孔为3.5mm规格。",
    "2019款16寸MacBook Pro的耳机孔为2.5mm规格。",
    "2019款16寸MacBook Pro的耳机孔为3.5cm规格。",
    "笔记本耳机孔为3.5mm规格。",
    "2019款16寸MacBook Pro的耳机孔为3.5mm规格，可通过补拍近景核验。",
])
def test_interface_shorthand_cannot_change_value_model_unit_or_add_a_step(body):
    from answer_hub.mimo import MimoLabelResult

    topics, _, _, _ = w.build_topic_review_rows([{
        "数据ID": "spec-boundary", "_原子知识ID": "spec-boundary-U1",
        "产品类型": "笔记本",
        "核心问题": "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
        "对象/部位": "耳机孔", "异常现象": "接口规格查询",
        "聊天内容": "回收师：2019款16寸MacBook Pro耳机孔是多大\n答疑：3.5的",
    }], mimo_client=SimpleNamespace(
        config=SimpleNamespace(model="test"),
        label_topic=lambda *a, **k: MimoLabelResult(candidate={
            "title": "笔记本耳机孔是什么规格", "subtitles": [],
            "content": body, "recommended_reply": body, "knowledge_form": "具体判定",
            "content_type": "定义型", "confidence": 0.95, "standard_refs": [],
            "applicable_scope": "笔记本", "applicable_brands": [], "applicable_models": [],
        }, request_audit={}, response_audit={}),
    ), use_mimo=True, clustering_mode="rule", use_standard_references=False,
       transcribe_all_admitted_topics=True)
    assert topics[0]["模型质量状态"] == "failed"


def test_verified_fact_does_not_treat_a_generated_target_as_a_conclusion():
    row = {
        "数据ID": "folding-target", "_原子知识ID": "folding-target-U1",
        "_人工答复门禁状态": "answered", "_原子实际回复": "判自动关机",
        "产品类型": "手机",
        "核心问题": "手机｜开机情况｜折叠屏一折就关机｜应勾选自动重启/关机而非无法回收",
        "对象/部位": "开机情况", "异常现象": "折叠后自动关机",
        "判定目标": "勾选自动重启/关机，不直接判无法回收",
        "聊天内容": "回收师：折叠屏一折就关机\n答疑：判自动关机",
    }
    payload = w._topic_stage_payload("folding-target", [row], use_standard_references=False)
    import json
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "判自动关机" in serialized
    assert "无法回收" not in serialized
    assert payload["evidence_package"]["facts"][0]["condition"] == ""


@pytest.mark.parametrize("body,supported,source_question", [
    ("折叠屏手机出现折叠后自动关机的现象时，开机情况按自动关机判定。", True, None),
    ("折叠屏手机在折叠动作后出现自动关机时，开机情况按自动关机判定。", True, None),
    ("折叠屏手机在折叠动作后出现自动关机时，开机情况质检项按自动关机判定。", True, None),
    ("折叠屏手机在折叠动作前出现自动关机时，开机情况按自动关机判定。", False, None),
    ("折叠屏手机在折叠动作后不出现自动关机时，开机情况按自动关机判定。", False, None),
    ("折叠屏手机电量正常且出现折叠后自动关机的现象时，开机情况按自动关机判定。", False, None),
    ("折叠屏手机出现折叠后自动关机一次时，开机情况按自动关机判定。", False, None),
    ("折叠屏手机出现折叠后自动关机的现象时，按无法回收判定。", False, None),
    ("折叠屏手机出现折叠后自动关机的现象时，开机情况按自动关机判定。", False,
     "手机｜开机情况｜电量超过50%时折叠屏一折就关机"),
    ("折叠屏手机出现折叠后自动关机的现象时，开机情况按自动关机判定。", False,
     "手机｜开机情况｜三星W23折叠后关机"),
])
def test_verified_short_disposition_can_be_written_as_a_complete_rule(body, supported, source_question):
    import json

    from answer_hub.mimo import MimoLabelResult

    calls = []

    def label_topic(topic, matches, *, retry_reason=None, **kwargs):
        calls.append(retry_reason)
        return MimoLabelResult(candidate={
            "title": "折叠屏手机折叠后自动关机怎么判定",
            "subtitles": [], "content": body, "recommended_reply": "判自动关机。",
            "knowledge_form": "具体判定", "content_type": "定义型",
            "confidence": 0.95, "standard_refs": [], "applicable_scope": "手机",
            "applicable_brands": [], "applicable_models": [],
        }, request_audit={}, response_audit={})

    topics, _, _, _ = w.build_topic_review_rows([{
        "数据ID": "short-answer", "_原子知识ID": "short-answer-U1",
        "产品类型": "手机",
        "核心问题": source_question or "手机｜开机情况｜折叠屏一折就关机｜判定对应选项",
        "对象/部位": "开机情况", "异常现象": "折叠后自动关机",
        "聊天内容": f"回收师：{source_question or '折叠屏一折就关机'}\n答疑：判自动关机",
    }], mimo_client=SimpleNamespace(
        config=SimpleNamespace(model="test"), label_topic=label_topic,
    ), use_mimo=True, clustering_mode="rule", use_standard_references=False,
       transcribe_all_admitted_topics=True)
    assert len(topics) == 1
    if supported:
        assert body in topics[0]["知识内容"]
        assert topics[0]["推荐回复"] == body
        assert not topics[0]["主题无来源内容"]
        assert json.loads(topics[0]["转写质量审计"])["status"] == "passed"
        assert topics[0]["模型初标结论"] == "通过"
        assert calls == [None]
    else:
        assert topics[0]["模型质量状态"] == "failed"


@pytest.mark.parametrize("title, accepted", [
    ("折叠屏手机折叠后关机应选自动重启/关机吗", True),
    ("折叠屏手机折叠后关机应选自动重启／关机吗", True),
    ("手机/电池/外观怎么判定", False),
])
def test_transcription_accepts_a_business_option_slash_without_allowing_keyword_stacks(title, accepted):
    import json

    from answer_hub.mimo import MimoClient, MimoConfig, MimoError

    client = MimoClient(MimoConfig(api_key="test", base_url="https://example.com/v1", model="test"))
    candidate = {
        "title": title, "subtitles": [], "content": "1. 折叠屏手机折叠后关机时按自动关机判定。",
        "recommended_reply": "折叠后关机按自动关机判定。",
        "category_l1": "手机", "category_l2": "开机情况", "layer": "L2",
        "knowledge_form": "具体判定", "content_type": "定义型",
        "applicable_scope": "手机", "standard_refs": [],
        "reasoning_summary": "人工最终答复为判自动关机。",
        "image_evidence_summary": "无图片", "confidence": 0.95,
    }
    client._post = lambda payload: {  # type: ignore[method-assign]
        "choices": [{"message": {"content": json.dumps(candidate, ensure_ascii=False)}}],
    }
    if accepted:
        result = client.label_topic({}, [], use_standard_references=False, max_attempts=1)
        assert result.candidate["title"] == title
    else:
        with pytest.raises(MimoError, match="关键词堆砌"):
            client.label_topic({}, [], use_standard_references=False, max_attempts=1)


@pytest.mark.parametrize("decision", ["需修改", "证据不足待补充", "驳回"])
def test_quality_review_can_preserve_an_unworthy_value_with_a_nonpassing_decision(decision):
    import json

    from answer_hub.mimo import MimoClient, MimoConfig

    client = MimoClient(MimoConfig(api_key="test", base_url="https://example.com/v1", model="test"))
    client._post = lambda payload: {  # type: ignore[method-assign]
        "choices": [{"message": {"content": json.dumps({
            "decision": decision, "knowledge_value": "不值得沉淀",
            "error_type": "证据不足", "reason": "原样保留转写前沉淀价值，草稿仍需补充事实。",
            "standard_consistency": "无可信标准", "evidence_sufficiency": "不足",
            "content_consistency": "部分一致", "image_necessity": "不需要",
            "title_quality": "清晰", "confidence": 0.9, "priority_review": True,
        }, ensure_ascii=False)}}],
    }
    result = client.review_topic(
        {"topic_id": "review-fixture", "knowledge_value": "不值得沉淀"},
        {"主标题": "折叠后关机如何判定", "知识内容": "1. 按自动关机判定。"},
        [], use_standard_references=False,
    )
    assert result.candidate["decision"] == decision
    assert result.candidate["knowledge_value"] == "不值得沉淀"


@pytest.mark.parametrize("full_transcription", [False, True])
def test_soft_cluster_risk_can_receive_a_model_draft_while_remaining_pending(
    monkeypatch, full_transcription,
):
    from answer_hub.mimo import MimoLabelResult

    row = {
        "数据ID": "soft-risk", "_原子知识ID": "soft-risk-U1",
        "产品类型": "手机", "回收业务层级": "自营回收",
        "核心问题": "折叠屏手机折叠后关机如何判定",
        "对象/部位": "开机情况", "异常现象": "折叠后关机",
        "问题意图": "标准判定", "语义标注置信度": 0.6,
        "_原子需要复核": True,
        "聊天内容": "回收师：折叠屏手机折叠后关机如何判定\n答疑：判自动关机",
    }
    monkeypatch.setattr(w, "_direct_mimo_topic_groups", lambda *a, **k: (
        [(("frozen-soft-risk",), [row])], {"effective_mode": "direct_mimo"},
    ))
    calls = []

    def stage(payload):
        calls.append("stage")
        candidate = w._rule_topic_stage_classification(payload)
        candidate.update(knowledge_value="值得沉淀", needs_human_review=True)
        return SimpleNamespace(candidate=candidate, request_audit={}, response_audit={})

    def label(topic, matches, **kwargs):
        calls.append("label")
        return MimoLabelResult(candidate={
            "title": "折叠屏手机折叠后关机如何判定", "subtitles": [],
            "content": "折叠屏手机在折叠后关机时，按自动关机判定。",
            "recommended_reply": "折叠后关机按自动关机判定。",
            "knowledge_form": "具体判定", "content_type": "定义型",
            "confidence": 0.95, "standard_refs": [], "applicable_scope": "手机",
            "applicable_brands": [], "applicable_models": [],
        }, request_audit={}, response_audit={})

    def review(*args, **kwargs):
        calls.append("review")
        return SimpleNamespace(candidate={
            "decision": "通过", "knowledge_value": "值得沉淀", "error_type": "",
            "reason": "人工实答支持处理结论", "standard_consistency": "无可信标准",
            "evidence_sufficiency": "充分", "content_consistency": "一致",
            "image_necessity": "不需要", "title_quality": "清晰",
            "confidence": 0.95, "priority_review": False,
        }, request_audit={}, response_audit={})

    topics, mapping, gaps, pending = w.build_topic_review_rows(
        [row], mimo_client=SimpleNamespace(
            config=SimpleNamespace(model="test"),
            classify_topic_stage=stage, label_topic=label, review_topic=review,
        ),
        use_mimo=True, clustering_mode="direct_mimo", use_standard_references=False,
        enforce_cluster_admission=True, transcribe_all_admitted_topics=full_transcription,
    )
    assert not gaps and not pending
    assert len(topics) == len(mapping) == 1
    topic = topics[0]
    assert topic["主题状态"] == "provisional_singleton_review_pending"
    assert topic["聚类准入状态"] == "暂定单主题候选"
    assert topic["是否重点复核"] == "是"
    assert topic["自动审核状态"] != "auto_approved"
    assert "未获得明确人工通过" in w._topic_candidate_export_gate_issues(
        topic, use_standard_references=False,
    )
    if full_transcription:
        assert calls[0] == "stage" and "label" in calls and calls[-1] == "review"
        assert topic["主题模型提供方"] == "mimo"
        assert "折叠" in topic["知识内容"] and "自动关机" in topic["知识内容"]
    else:
        assert calls == []
