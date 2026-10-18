from answer_hub.workflow import _recommended_reply


def test_reply_omits_empty_greeting_fragment():
    reply = _recommended_reply(
        "回收结论", "", existing_reply="您好，；\n可正常回收",
        use_standard_references=False,
    )
    assert reply.startswith("您好，可正常回收。")
    assert reply.count("您好") == 1
