import pytest

from answer_hub.answer_evidence import verify_human_answer


@pytest.mark.parametrize("answer", ["", "稍等", "好的", "[图片]", "不清楚", "我去催一下"])
def test_missing_or_nonanswer_cannot_be_replaced_with_reference(answer):
    row = {
        "核心问题": "内存是否为品牌", "参考话术": "使用工具确认内存品牌。",
        "判定结论": "内存是三星。",
        "聊天内容": f"回收师：内存是否为品牌？\n答疑：{answer}",
    }
    assert verify_human_answer(row).status == "missing"


def test_one_answer_does_not_admit_sibling_question():
    base = {
        "_原子来源问题数": 3,
        "聊天内容": "回收师：内存和硬盘是不是品牌？\n回收师：没有指纹选项\n答疑：不支持指纹",
    }
    assert verify_human_answer({**base, "核心问题": "内存品牌"}).status == "missing"
    assert verify_human_answer({**base, "核心问题": "指纹功能"}).answer == "不支持指纹"


def test_final_human_answer_does_not_include_generated_extra_conditions():
    evidence = verify_human_answer({
        "核心问题": "折叠屏折叠后关机如何选择",
        "参考话术": "电量正常、未设置定时关机，出现一次即可选自动关机。",
        "聊天内容": "回收师：折叠屏一折就关机\n答疑：无法回收\n答疑：稍等\n答疑：判自动关机",
    })
    assert evidence.answer == "判自动关机"


def test_reordered_multi_answer_is_scoped_by_object():
    evidence = verify_human_answer({
        "_原子来源问题数": 2, "核心问题": "后壳SN是否原装",
        "聊天内容": "回收师：硬盘品牌和后壳SN怎么判断\n答疑：SN号一致是原装；硬盘需要补图",
    })
    assert evidence.answer == "SN号一致是原装"


@pytest.mark.parametrize("answer", [
    "内存如何判断", "您好，正在为您转接人工客服",
    "请稍候，说明一下你的问题和现场质检情况，我立即为你核对相关信息，稍后给你准确答复。",
])
def test_questions_and_automatic_holding_messages_are_not_answers(answer):
    assert verify_human_answer({
        "核心问题": "内存是否品牌", "聊天内容": f"回收师：内存是否品牌\n答疑：{answer}",
    }).status == "missing"


def test_same_component_different_target_is_not_an_answer():
    row = {
        "_原子来源问题数": 2, "核心问题": "电池是否原装",
        "聊天内容": "回收师：电池是否原装\n回收师：电池健康度80%怎么判\n答疑：电池健康度80%按损耗处理",
    }
    assert verify_human_answer(row).status == "missing"


def test_full_stop_separates_answers_to_sibling_objects():
    row = {
        "_原子来源问题数": 2, "核心问题": "内存品牌",
        "聊天内容": "回收师：内存与硬盘怎么判\n答疑：内存是三星。硬盘按第三方更换处理。",
    }
    assert verify_human_answer(row).answer == "内存是三星。"


def test_explicit_actual_reply_is_usable_with_question_only_chat():
    assert verify_human_answer({
        "核心问题": "内存品牌如何判断", "聊天内容": "回收师：内存品牌如何判断",
        "实际回复": "内存是三星",
    }).answer == "内存是三星"


def test_manual_correction_conflict_requires_human_decision():
    evidence = verify_human_answer({
        "核心问题": "内存是否更换", "人工判定结论": "内存必须更换",
        "聊天内容": "回收师：内存需要更换吗\n答疑：内存无需更换",
    })
    assert evidence.status == "conflict"
    assert evidence.answer == "内存无需更换"


def test_same_component_different_phenomenon_is_not_an_answer():
    assert verify_human_answer({
        "_原子来源问题数": 2, "核心问题": "屏幕划痕如何判定",
        "聊天内容": "回收师：屏幕划痕如何判定\n回收师：屏幕碎裂如何判定\n答疑：屏幕碎裂按碎屏处理",
    }).status == "missing"


@pytest.mark.parametrize("chat", [
    "回收师：内存是不是品牌\n答疑：您好，很高兴为您服务",
    "回收师：内存是不是品牌\n答疑：内存是否可以正常回收",
    "回收师：内存是不是品牌\n机器人客服：内存是三星",
    "回收师：内存是不是品牌\n答疑：稍等\n机器人客服：内存是三星",
])
def test_greeting_question_or_robot_does_not_establish_human_answer(chat):
    assert verify_human_answer({"核心问题": "内存是不是品牌", "聊天内容": chat}).status == "missing"


def test_answer_before_closing_question_is_preserved_by_source_filter():
    from answer_hub.answer_evidence import has_any_human_answer
    row = {"核心问题": "内存品牌", "聊天内容": "回收师：内存品牌\n答疑：内存是三星。还有什么问题？"}
    assert has_any_human_answer(row)
    assert verify_human_answer(row).answer == "内存是三星。"


def test_original_vs_replacement_conflict_is_not_silently_overwritten():
    assert verify_human_answer({
        "核心问题": "内存是否原装", "人工判定结论": "内存是第三方更换",
        "聊天内容": "回收师：内存是否原装\n答疑：内存是原装",
    }).status == "conflict"


def test_same_negative_conclusion_is_not_a_conflict():
    assert verify_human_answer({
        "核心问题": "内存是否更换", "人工判定结论": "内存不需要更换",
        "聊天内容": "回收师：内存是否更换\n答疑：内存不需要更换",
    }).status == "answered"


def test_support_status_description_does_not_contradict_unsupported_answer():
    assert verify_human_answer({
        "核心问题": "指纹功能是否支持",
        "判定结论": "确认不支持指纹，无需按指纹功能支持状态进行判定。",
        "聊天内容": "回收师：指纹怎么判\n答疑：不支持指纹",
    }).status == "answered"


def test_acknowledgment_does_not_erase_question_context():
    evidence = verify_human_answer({
        "_原子来源问题数": 2, "核心问题": "后摄镜头金属圈是磕点还是掉漆",
        "聊天内容": "回收师：镜头那个是磕点还是掉漆\n答疑：金属圈上面么\n回收师：嗯\n答疑：算磕碰",
    })
    assert evidence.status == "answered"
    assert "算磕碰" in evidence.answer


@pytest.mark.parametrize("link", [
    "https://example.com/?locale=zh_CN",
    "[官网](https://example.com/?locale=zh_CN)",
    "![截图](https://example.com/?locale=zh_CN)",
])
def test_url_query_fragment_cannot_establish_a_human_answer(link):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "核心问题": "官网链接在哪里", "聊天内容":
        f"回收师：官网链接在哪里\n答疑：{link}",
    }
    assert not has_any_human_answer(row)
    assert verify_human_answer(row).status == "missing"


@pytest.mark.parametrize("link", [
    "https://example.com/?locale=zh_CN",
    "[官网](https://example.com/?locale=zh_CN)",
])
def test_rule_surrounding_url_remains_available(link):
    evidence = verify_human_answer({
        "核心问题": "内存品牌如何查询",
        "聊天内容": "回收师：内存品牌如何查询\n"
        f"答疑：内存品牌以官网查询结果为准 {link}",
    })
    assert evidence.status == "answered"
    assert evidence.answer == "内存品牌以官网查询结果为准"


@pytest.mark.parametrize("answer", ["3.5的", "0.5mm按磕碰", "1. 内存是三星"])
def test_leading_decimal_in_an_actual_answer_is_not_a_list_number(answer):
    evidence = verify_human_answer({
        "核心问题": "内存或耳机孔如何确认",
        "聊天内容": f"回收师：内存或耳机孔如何确认\n答疑：{answer}",
    })
    expected = "内存是三星" if answer.startswith("1. ") else answer
    assert evidence.answer == expected


def test_water_indicator_discoloration_does_not_borrow_a_missing_label_answer():
    evidence = verify_human_answer({
        "_原子来源问题数": 2,
        "核心问题": "主板防水标签变红如何判定", "对象/部位": "防水标",
        "异常现象": "变红",
        "聊天内容": "回收师：后壳更换后防水标没有了，要判吗\n答疑：换过后壳，不判后壳防水标缺失",
    })
    assert evidence.status == "missing"


def test_water_indicator_state_guard_does_not_change_display_color_synonyms():
    evidence = verify_human_answer({
        "核心问题": "屏幕色斑如何判定",
        "聊天内容": "回收师：屏幕色斑如何判定\n答疑：屏幕变色按显示异常判",
    })
    assert evidence.answer == "屏幕变色按显示异常判"


@pytest.mark.parametrize("answer", [
    "稍等我看看哈", "我看看", "有无换后壳", "拍全看一下", "官网发下", "官网",
    "有几个卡口选",
])
def test_reception_and_clarifying_questions_are_not_reusable_answers(answer):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "核心问题": "后壳刻字如何判定",
        "聊天内容": f"回收师：后壳刻字如何判定\n答疑：{answer}",
    }
    assert not has_any_human_answer(row)
    assert verify_human_answer(row).status == "missing"


def test_interface_size_and_failure_answers_remain_in_their_own_questions():
    chat = (
        "回收师：耳机孔是多大\n答疑：3.5的\n"
        "回收师：耳机插不进去判啥\n答疑：耳机接口功能异常(失灵/变形导致无法使用)"
    )
    size = verify_human_answer({
        "核心问题": "耳机孔规格查询，确认接口口径", "聊天内容": chat,
        "_原子来源问题数": 2,
    })
    failure = verify_human_answer({
        "核心问题": "耳机接口无法插入如何判定", "聊天内容": chat,
        "_原子来源问题数": 2,
    })
    assert size.answer == "3.5的"
    assert failure.answer == "耳机接口功能异常(失灵/变形导致无法使用)"


def test_sales_region_question_does_not_borrow_an_ear_tip_replacement_answer():
    evidence = verify_human_answer({
        "核心问题": "耳机购买渠道和销售地如何判定是否为国行",
        "_原子来源问题数": 2,
        "聊天内容": "回收师：耳机字体是中文的\n答疑：正品耳机，判定更换耳套",
    })
    assert evidence.status == "missing"


@pytest.mark.parametrize("question,answer", [
    ("耳机购买渠道和销售地是否为国行", "官网发货的是国行"),
    ("内存品牌如何确认", "图片提供的内存是三星"),
])
def test_complete_answer_about_supplied_material_is_not_a_request(question, answer):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {"核心问题": question, "聊天内容": f"回收师：{question}\n答疑：{answer}"}
    assert has_any_human_answer(row)
    assert verify_human_answer(row).answer == answer


@pytest.mark.parametrize("answer", ["有没有设置定时关机", "有没有勾选自动关机"])
def test_a_clarification_is_not_an_answer_even_when_it_mentions_an_option(answer):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "核心问题": "折叠后关机如何勾选",
        "聊天内容": f"回收师：折叠后关机如何勾选\n答疑：{answer}",
    }
    assert not has_any_human_answer(row)
    assert verify_human_answer(row).status == "missing"


def test_continuing_question_keeps_the_object_for_a_short_confirmation():
    evidence = verify_human_answer({
        "_原子来源问题数": 2,
        "核心问题": "手机卡槽防水标缺失如何判定",
        "对象/部位": "防水标", "异常现象": "缺失",
        "聊天内容": "回收师：卡槽处的防水标缺失了\n"
                    "回收师：要不要判缺失呀\n答疑：要的",
    })
    assert evidence.status == "answered"
    assert evidence.answer == "要的"


@pytest.mark.parametrize("question,answer", [
    ("前置摄像头无法对焦如何处理", "后置摄像头划痕按镜头划伤处理。"),
    ("前置摄像头无法对焦如何处理", "前置摄像头划痕按镜头划伤处理。"),
    ("前置摄像头划痕如何判定", "前置摄像头无法对焦按功能异常处理。"),
    ("前置摄像头无法对焦如何处理", "后置摄像头无法对焦按功能异常处理。"),
])
@pytest.mark.parametrize("answer_field", ["聊天内容", "实际回复"])
def test_camera_answer_cannot_cross_atomic_phenomenon_or_position(
    question, answer, answer_field,
):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "_原子来源问题数": 2,
        "核心问题": question,
        "参考话术": "前置摄像头无法对焦按功能异常处理。",
        "聊天内容": f"回收师：{question}\n",
    }
    if answer_field == "聊天内容":
        row["聊天内容"] += f"回收师：另一个摄像头问题怎么判\n答疑：{answer}"
    else:
        row[answer_field] = answer

    assert has_any_human_answer(row)
    evidence = verify_human_answer(row)
    assert evidence.status == "missing"
    assert evidence.answer == ""


@pytest.mark.parametrize("answer", [
    "检查摄像头是否可以正常对焦。",
    "核验摄像头能否正常对焦。",
    "确认摄像头是否能够正常对焦。",
    "查看摄像头是否存在划痕。",
    "请先检查摄像头是否可以正常对焦。",
])
def test_explicit_check_instruction_is_a_human_answer(answer):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "核心问题": "摄像头功能和外观如何检查",
        "聊天内容": f"回收师：摄像头功能和外观如何检查\n答疑：{answer}",
    }
    assert has_any_human_answer(row)
    evidence = verify_human_answer(row)
    assert evidence.status == "answered"
    assert evidence.answer == answer


@pytest.mark.parametrize("answer", [
    "摄像头是否可以正常对焦",
    "摄像头能否正常对焦",
    "检查摄像头是否可以正常对焦？",
    "检查摄像头是否可以正常对焦吗",
    "有没有检查摄像头是否可以正常对焦",
    "检查摄像头是否。",
    "检查摄像头是否正常，要不要",
])
def test_check_clarification_does_not_establish_a_human_answer(answer):
    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "核心问题": "摄像头功能如何检查",
        "参考话术": "检查摄像头是否可以正常对焦。",
        "聊天内容": f"回收师：摄像头功能如何检查\n答疑：{answer}",
    }
    assert not has_any_human_answer(row)
    assert verify_human_answer(row).status == "missing"


@pytest.mark.parametrize("question,expected", [
    ("前置摄像头无法对焦如何处理", "前置摄像头无法对焦按功能异常处理。"),
    ("前置摄像头划痕如何判定", "前置摄像头划痕按镜头划伤处理。"),
])
def test_camera_reordered_answers_keep_their_atomic_owners(question, expected):
    evidence = verify_human_answer({
        "_原子来源问题数": 2,
        "核心问题": question,
        "聊天内容": "回收师：前置摄像头无法对焦和划痕分别如何处理\n"
                    "答疑：前置摄像头划痕按镜头划伤处理。"
                    "前置摄像头无法对焦按功能异常处理。",
    })
    assert evidence.status == "answered"
    assert evidence.answer == expected


@pytest.mark.parametrize("answer", [
    "检查后置摄像头是否可以正常对焦。",
    "检查前置摄像头是否存在划痕。",
    "检查电池是否需要更换。",
])
def test_check_instruction_still_requires_the_current_atomic_owner(answer):
    from copy import deepcopy

    from answer_hub.answer_evidence import has_any_human_answer

    row = {
        "_原子来源问题数": 2,
        "核心问题": "前置摄像头无法对焦如何处理",
        "聊天内容": "回收师：前置摄像头无法对焦如何处理\n"
                    f"答疑：{answer}",
    }
    original = deepcopy(row)
    assert has_any_human_answer(row)
    assert verify_human_answer(row).status == "missing"
    assert row == original


@pytest.mark.parametrize("question,answer", [
    ("前后摄像头无法对焦如何处理", "前置摄像头无法对焦按功能异常处理。"),
    ("前后摄像头无法对焦如何处理", "后置摄像头无法对焦按功能异常处理。"),
    ("前置摄像头无法对焦如何处理", "前后摄像头无法对焦按功能异常处理。"),
    ("后置摄像头无法对焦如何处理", "前后摄像头无法对焦按功能异常处理。"),
])
def test_camera_joint_scope_keeps_answers_for_each_included_position(question, answer):
    evidence = verify_human_answer({
        "_原子来源问题数": 2,
        "核心问题": question,
        "聊天内容": f"回收师：{question}\n答疑：{answer}",
    })
    assert evidence.status == "answered"
    assert evidence.answer == answer
