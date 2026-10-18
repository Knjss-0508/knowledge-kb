"""Minimal real turns from frozen R74/R114; no runtime artifact dependency."""

import pytest

from answer_hub.answer_evidence import verify_human_answer


R74_CHAT = (
    "回收师：字体应该没问题吧\n"
    "答疑：我看看\n"
    "答疑：耳机字体 定位发一下呢\n"
    "回收师：[图片]\n"
    "回收师：[图片]\n"
    "答疑：正品耳机 判定更换耳套"
)
R74_POLLUTED_ATOMIC_QUESTION = (
    "回收师现场遇到“耳机购买渠道无法确认怎么办”的质检问题，"
    "并补充上传充电盒、耳机字体标识及查找定位功能图片，询问“字体应该没问题吧”。"
    "核心诉求是确认该耳机购买渠道能否判定为国行，以及后续如何处理；"
    "答疑“正品耳机 判定更换耳套”未正面回应购买渠道确认问题。"
)
R114_CHAT = (
    "回收师：兄弟，19年MacBook Pro耳机孔是多大了\n"
    "答疑：3.5的\n"
    "回收师：插不进去判啥呢\n"
    "答疑：耳机接口功能异常(失灵/变形导致无法使用)"
)


@pytest.mark.parametrize(
    "atomic_question",
    ["", R74_POLLUTED_ATOMIC_QUESTION],
    ids=["canonical-fields-only", "quoted-unanswered-reply-in-atomic-summary"],
)
def test_r74_channel_has_no_actual_answer_even_with_quoted_sibling_reply(
    atomic_question,
):
    evidence = verify_human_answer({
        "_原子核心问题": atomic_question,
        "_原子来源问题数": 1,
        "核心问题": (
            "耳机/耳麦｜购买渠道｜爬虫或系统无法确认销售地｜"
            "依据充电盒与耳机简体中文标识判定国行"
        ),
        "对象/部位": "购买渠道（充电盒与耳机字体标识）",
        "异常现象": "销售地无法确认",
        "判定目标": "判定是否为国行",
        "聊天内容": R74_CHAT,
    })

    assert evidence.status == "missing"
    assert evidence.answer == ""
    assert evidence.reason


@pytest.mark.parametrize(
    "question_fields,expected_answer",
    [
        (
            {
                "核心问题": (
                    "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询｜"
                    "确认接口口径"
                ),
                "对象/部位": "耳机孔",
                "异常现象": "接口规格查询",
                "判定目标": "确认耳机孔口径",
            },
            "3.5的",
        ),
        (
            {
                "核心问题": "笔记本｜耳机接口｜耳机无法插入｜判定对应质检异常项",
                "对象/部位": "耳机接口",
                "异常现象": "无法插入耳机",
                "判定目标": "确定耳机孔插不进去对应的质检项",
            },
            "耳机接口功能异常(失灵/变形导致无法使用)",
        ),
    ],
    ids=["r114-u1-diameter-only", "r114-u2-failure-only"],
)
def test_r114_same_interface_answers_belong_to_their_own_question(
    question_fields, expected_answer,
):
    evidence = verify_human_answer({
        "_原子核心问题": "",
        "_原子来源问题数": 2,
        **question_fields,
        "聊天内容": R114_CHAT,
    })

    assert evidence.status == "answered"
    assert evidence.answer == expected_answer
    assert evidence.reason == ""


@pytest.mark.parametrize("short_answer", ["是的", "可以"])
def test_short_answer_to_matching_question_remains_usable(short_answer):
    evidence = verify_human_answer({
        "_原子来源问题数": 1,
        "核心问题": "内存是否为品牌内存",
        "对象/部位": "内存",
        "异常现象": "品牌属性待确认",
        "判定目标": "确认内存是否为品牌件",
        "聊天内容": f"回收师：这内存是品牌的吗\n答疑：{short_answer}",
    })

    assert evidence.status == "answered"
    assert evidence.answer == short_answer
    assert evidence.reason == ""
