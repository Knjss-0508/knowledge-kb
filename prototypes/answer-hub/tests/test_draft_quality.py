from answer_hub.draft_quality import assess_case_only_draft


def test_case_only_draft_requires_source_specific_conclusion() -> None:
    result = assess_case_only_draft(
        content=(
            "适用主题：笔记本转轴异响。\n"
            "1. 确认异常对象和出现条件。\n"
            "2. 补充图片或视频后再判定。\n"
            "3. 结合案例证据确认处理边界。"
        ),
        source_values=[
            "闭合瞬间的单次异响也属于转轴异响。",
            "其他声音情形仍需补充对应来源证据。",
        ],
    )

    assert result.decision == "manual_review"
    assert result.reasons == ("正文只有通用模板，未使用来源中的具体事实或结论。",)


def test_case_only_draft_accepts_supported_specific_conclusion_with_boundary() -> None:
    result = assess_case_only_draft(
        content=(
            "闭合瞬间出现单次异响时，可按来源结论归为转轴异响。\n"
            "本知识仅覆盖该出现时机；其他声音情形仍需补充对应来源证据。"
        ),
        source_values=[
            "闭合瞬间的单次异响也属于转轴异响。",
            "其他声音情形仍需补充对应来源证据。",
        ],
    )

    assert result.decision == "pass"
    assert result.reasons == ()


def test_copied_generic_source_reply_is_not_source_specific_evidence() -> None:
    template = (
        "适用主题：基本情况 / 使用情况\n"
        "明确待确认的对象、现象和对应问题。\n"
        "补充支持判断的截图、照片、视频或查询结果。\n"
        "结合案例证据确认适用条件、边界和例外。\n"
        "无法明确判断时，补充证据后再判定。"
    )
    result = assess_case_only_draft(content=template, source_values=[template])
    assert result.decision == "manual_review"


def test_synonym_generic_reply_copied_from_source_still_needs_review() -> None:
    template = (
        "适用主题：笔记本转轴异响。\n"
        "1. 确认异常对象和出现条件。\n"
        "2. 补充图片或视频后再判定。\n"
        "3. 结合案例证据确认处理边界。"
    )
    result = assess_case_only_draft(content=template, source_values=[template])
    assert result.decision == "manual_review"


def test_generic_sentences_without_question_are_not_specific_source_facts() -> None:
    template = (
        "确认异常对象和出现条件。\n"
        "补充图片或视频后再判定。\n"
        "结合案例证据确认处理边界。"
    )
    result = assess_case_only_draft(content=template, source_values=[template])
    assert result.decision == "manual_review"


def test_specific_source_fact_survives_generic_sentence_filter() -> None:
    fact = "闭合瞬间的单次异响也属于转轴异响"
    result = assess_case_only_draft(
        content=(
            "确认异常对象和出现条件。\n"
            "补充图片或视频后再判定。\n"
            "结合案例证据确认处理边界。\n"
            + fact
        ),
        source_values=[fact],
    )
    assert result.decision == "pass"
