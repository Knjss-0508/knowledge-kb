from answer_hub import workflow


TITLE = "设备信息读取与交叉核对流程"
CONTENT = (
    "先进入手机设备信息页面，读取当前设备完整型号、系统版本和存储配置，并记录页面显示的原始信息。"
    "使用检测工具读取同一设备的型号、系统版本和存储配置，逐项比较两处显示的信息是否一致。"
    "两处信息不一致时保留设备页面截图和检测工具结果，核对后再记录设备配置，不凭单一来源直接修改。"
)


def test_long_body_fallback_produces_reply_that_survives_final_quality_gate():
    result = workflow._select_topic_recommended_reply(
        [{"数据ID": "synthetic-1", "实际回复": CONTENT}],
        title=TITLE,
        content=CONTENT,
        model_reply="",
        evidence_status="available",
        use_standard_references=False,
    )
    reply = result["reply"]
    assert reply
    assert workflow._recommended_reply_quality_issues(
        reply, title=TITLE, content=CONTENT,
    ) == []
    assert "两处信息不一致" in reply
    assert "不凭单一来源直接修改" in reply


def test_topic_export_keeps_grounded_reply_when_model_reply_is_missing():
    row = {
        "数据ID": "synthetic-1",
        "工单ID": "synthetic-1",
        "产品类型": "手机",
        "核心问题": "设备信息如何读取与交叉核对",
        "对象/部位": "设备信息",
        "异常现象": "读取结果需核对",
        "_原子实际回复": CONTENT,
        "_人工答复门禁状态": "answered",
        "聊天内容": "回收师：设备信息怎么核对？\n答疑：" + CONTENT,
    }
    topic = workflow._topic_candidate_row(
        "synthetic-topic", ("手机", "设备信息"), [row], [],
        {
            "title": TITLE,
            "content": CONTENT,
            "recommended_reply": "",
            "confidence": 0.95,
            "knowledge_form": "流程方法",
            "content_type": "核验型",
            "standard_refs": [],
        },
        "mimo", "synthetic-model", "synthetic-prompt", "synthetic-run",
        "", "topic_model_labeled", 0.75,
        use_standard_references=False,
    )
    assert topic["推荐回复"]
    assert "两处信息不一致" in topic["推荐回复"]
    assert "不凭单一来源直接修改" in topic["推荐回复"]
    assert "推荐回复为空" not in str(topic)
