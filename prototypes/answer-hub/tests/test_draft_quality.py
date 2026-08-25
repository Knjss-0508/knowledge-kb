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
