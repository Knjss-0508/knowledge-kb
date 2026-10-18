"""Verify that an answer was actually given to this question by a human."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_OBJECTS = {
    "内存": ("内存", "ram", "运行内存"),
    "硬盘": ("硬盘", "ssd", "固态", "存储盘"),
    "指纹": ("指纹", "生物识别"),
    "电池": ("电池", "健康度", "校准中"),
    "机型": ("机型", "型号", "卫星通信版", "小型号"),
    "序列号": ("序列号", "sn码", "sn号", "后壳sn"),
    "保护膜": ("保护膜", "贴膜"),
    "摄像头": ("摄像头", "后摄", "镜头"),
    "排线": ("排线", "折屏线"),
    "偏光": ("偏光",),
    "胶条": ("胶条", "脱胶"),
    "红线": ("红线",),
    "渠道": ("购买渠道", "国行", "海外版", "za版本"),
    "显示": ("屏幕", "色斑", "漏液", "显示异常", "成像异常", "坏点"),
    "防水标": ("防水标", "液触指示器", "浸液"),
    "激光雷达": ("激光雷达",),
    "气密": ("气密",),
    "关机": ("关机", "重启"),
}
_ROLE = re.compile(
    r"(?<![\w\u4e00-\u9fff])(?P<role>回收师|答疑(?:人员)?|人工客服|"
    r"机器人客服|AI客服|智能客服|机器人|系统|客服|用户|客户)"
    r"\s*[：:]\s*(?P<text>.*?)(?=(?:\n[^\n]*?)?"
    r"(?<![\w\u4e00-\u9fff])(?:回收师|答疑(?:人员)?|人工客服|机器人客服|"
    r"AI客服|智能客服|机器人|系统|客服|用户|客户)\s*[：:]|\Z)",
    re.DOTALL,
)
_FILLER = re.compile(
    r"^(?:稍等(?:一下)?(?:我看看[哈呢]?)?|我看看[哈呢]?|请稍等|好的?|嗯[嗯呢]?|收到|谢谢|不客气|"
    r"我(?:去|帮你)?催(?:一下)?|已催|催一下|报备培训师|官网|"
    r"不清楚|不知道|无法确认|暂时无法判断|[^\w\u4e00-\u9fff]*)[。！!，,\s]*$"
)
_NONANSWER = re.compile(
    r"请稍候|稍后给你|说明一下你的问题|我(?:快速|立即|帮你)?(?:核实|核对)|"
    r"^把疑问的位置标记|^(?:拍清晰|标记一下|图片看不出|这里有区分办法)|"
    r"^拍全看一下|^[^。！？!?]{0,16}(?:官网|定位|照片|图片|视频|截图|[a-dA-D]面|后盖小型号)"
    r"\s*(?:发|拍|提供|补充)(?:一下|下|来|给我|过来|看下|看一下)*[呢哈哦吧啊。！!\s]*$|"
    r"正在为您|转接人工|正在核实|请等待"
)
_PHENOMENA = {
    "划痕": ("划痕", "刮痕", "划伤"),
    "碎裂": ("碎裂", "碎屏", "破裂"),
    "磕碰": ("磕碰", "磕点", "凹痕", "掉漆"),
    "色斑": ("色斑", "紫色圆斑", "偏色"),
    "漏液": ("漏液",),
    "亮线": ("屏幕线", "竖线", "亮线"),
    "鼓包": ("鼓包", "膨胀"),
    "发热": ("发热", "过热"),
    "校准": ("校准", "评估中"),
    "容量健康": ("容量", "健康度", "损耗"),
    "对焦": ("对焦", "聚焦", "失焦", "跑焦"),
}
_CAMERA_POSITION = re.compile(r"(前后|前|后)(?:置)?(?:摄像头|摄|镜头)")
_TARGETS = {
    "品牌识别": ("品牌", "三星", "海力士", "镁光", "micron", "samsung", "hynix"),
    "更换原装": ("原装", "原厂", "第三方", "更换", "正品部件"),
    "健康容量": ("健康", "容量", "损耗", "校准", "评估中"),
    "工具读取": ("工具读", "读取异常", "一根线"),
    "进灰": ("进灰", "灰尘", "异物"),
    "外观损伤": ("磕碰", "掉漆", "磕点", "碎裂", "划痕"),
    "接口口径": ("接口口径", "孔径", "耳机孔规格", "3.5"),
    "接口故障": ("无法插入", "插不进去", "功能异常", "无法使用", "失灵"),
    "销售地区": ("销售地", "销售地区", "购买渠道", "国行", "海外版"),
}


def object_keys(text: Any) -> set[str]:
    value = str(text or "").lower()
    keys = {key for key, aliases in _OBJECTS.items() if any(a in value for a in aliases)}
    if keys & {"偏光", "胶条", "红线"}:
        keys.discard("显示")
    return keys


def question_text(row: dict[str, Any]) -> str:
    # The source summary can quote an unrelated answer to explain that a
    # question was not answered. It must not widen the canonical target.
    core = row.get("核心问题") or row.get("_原子核心问题")
    return " ".join(str(value or "") for value in (
        core, row.get("对象/部位"), row.get("异常现象"), row.get("判定目标"),
    ))


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _fragment_conflicts(question: str, answer: str) -> bool:
    qpositions = set("".join(_CAMERA_POSITION.findall(question)))
    apositions = set("".join(_CAMERA_POSITION.findall(answer)))
    if qpositions and apositions and qpositions.isdisjoint(apositions):
        return True
    if "防水标" in object_keys(question):
        states = {"变色": ("变红", "发红", "变色"), "缺失": ("缺失", "缺损", "没有了")}
        qstates = {key for key, terms in states.items() if any(term in question for term in terms)}
        astates = {key for key, terms in states.items() if any(term in answer for term in terms)}
        if qstates and astates and qstates.isdisjoint(astates):
            return True
    qphenomena = {key for key, terms in _PHENOMENA.items() if any(t in question.lower() for t in terms)}
    aphenomena = {key for key, terms in _PHENOMENA.items() if any(t in answer.lower() for t in terms)}
    if qphenomena and aphenomena and qphenomena.isdisjoint(aphenomena):
        return True
    qtargets = {key for key, terms in _TARGETS.items() if any(t in question.lower() for t in terms)}
    atargets = {key for key, terms in _TARGETS.items() if any(t in answer.lower() for t in terms)}
    if qtargets and atargets and qtargets.isdisjoint(atargets):
        return True
    return False


def fragment_matches(question: str, answer: str) -> bool:
    if _fragment_conflicts(question, answer):
        return False
    qkeys, akeys = object_keys(question), object_keys(answer)
    if qkeys and akeys:
        return bool(qkeys & akeys)
    return len(_bigrams(question) & _bigrams(answer)) >= 2


def split_answer_fragments(text: str) -> list[str]:
    # Strip the complete URL before splitting on sentence punctuation, so a
    # query string such as "?locale=zh_CN" cannot become an answer fragment.
    text = re.sub(
        r"!?\[[^\]\n]*\]\(\s*https?://[^)\s]+"
        r"(?:\s+['\"][^'\"]*['\"])?\s*\)",
        "", str(text or ""),
    )
    text = re.sub(r"https?://[^\s<>，。；！？]+", "", text)
    return [
        re.sub(r"^\s*\d+(?:、|\.(?!\d))\s*", "", part).strip()
        for part in re.split(r"[\n；;]+|(?<=[。！？!?])\s*", text)
        if part.strip()
    ]


def _substantive(text: str) -> bool:
    text = re.sub(r"\[(?:图片|视频|语音)\]", "", text).strip()
    # An imperative can contain an embedded question without asking the
    # customer a clarification, e.g. "检查摄像头是否可以正常对焦".
    check_instruction = bool(re.match(
        r"^(?:请|先|需要|应当|应|需)?(?:先)?"
        r"(?:检查|确认|核验|查看|检测|测试|复测)"
        r"[^，,。；;\n！？!?]{0,40}(?:是否|能否)[^，,。；;\s！？!?]",
        text,
    ))
    question_markers = re.sub(r"是否|能否", "", text) if check_instruction else text
    if re.match(r"^(?:有无|有没有|有几个|有哪些)", text) and not re.search(
        r"[，,；;。].{0,40}(?:判定|勾选|按.{1,24}(?:处理|判)|以.{1,40}为准)|"
        r"(?:均|都)(?:按|应|需|要|可)", text,
    ):
        return False
    if re.fullmatch(
        r"(?:您好|你好)?[，,\s]*(?:很高兴为您服务|有什么可以帮您|有什么可以帮你|"
        r"为您服务|欢迎咨询)[。！!\s]*", text,
    ):
        return False
    if re.search(r"是否|能否|是不是|要不要|可不可以|对不对", question_markers) and not re.search(
        r"(?:先|需要|请|应).{0,16}(?:确认|查看|核验|检查)", text,
    ):
        return False
    if re.search(r"如何|怎么|什么|为何|哪里|哪个|能否|是否", question_markers) and not re.search(
        r"判为|判定为|按.{1,24}(?:处理|判|勾选)|勾选|选择|属于|以.{1,40}为准|"
        r"不支持|可以|(?:需|先).{0,30}(?:查看|确认|核验)|(?:是|为)(?:三星|海力士|品牌)",
        text,
    ):
        return False
    return bool(
        text and not _FILLER.fullmatch(text) and not _NONANSWER.search(text)
        and not text.endswith(("？", "?"))
        and not re.search(r"(?:吗|么)[。！!\s]*$", text)
    )


@dataclass(frozen=True)
class HumanAnswerEvidence:
    status: str
    answer: str
    reason: str


def verify_human_answer(row: dict[str, Any]) -> HumanAnswerEvidence:
    """Reference replies and AI conclusions never establish a human answer."""
    question = question_text(row)
    chat = str(row.get("聊天内容") or "")
    turns = list(_ROLE.finditer(chat))
    count = row.get("_原子来源问题数") or 1
    single = str(count) == "1"
    answers: list[str] = []
    context = ""
    for turn in turns:
        text = turn.group("text").strip()
        if turn.group("role") in {"回收师", "用户", "客户"}:
            context_text = re.sub(r"\[(?:图片|视频|语音)\]", "", text).strip()
            if context_text and not _FILLER.fullmatch(context_text):
                if (
                    context
                    and not object_keys(context_text)
                    and re.match(r"^(?:要不要|需不需要|这个|这样|这种|算不算|判不判)", context_text)
                ):
                    context = f"{context}\n{text}"
                else:
                    context = text
            continue
        if turn.group("role") not in {"答疑", "答疑人员", "人工客服", "客服"}:
            continue
        for fragment in split_answer_fragments(text):
            if not _substantive(fragment):
                continue
            if _fragment_conflicts(question, fragment):
                continue
            keys = object_keys(fragment)
            matches = fragment_matches(question, fragment)
            # An explicit answer about a different object cannot be attached
            # through a nearby question or a shared question index.
            if matches or (
                not keys and fragment_matches(question, context)
            ):
                answers.append(fragment)
    if not answers:
        for field in ("_原子实际回复", "实际回复", "答疑回复", "回复内容", "客服回复", "历史实际回复"):
            if field == "历史实际回复" and row.get("历史实际回复来源类型") == "second_part_reference_reply":
                continue
            raw = str(row.get(field) or "")
            fragments = [p for p in split_answer_fragments(raw) if _substantive(p)]
            answers.extend(
                p for p in fragments
                if not _fragment_conflicts(question, p)
                and (
                    fragment_matches(question, p)
                    or single and (not object_keys(p) or not object_keys(question))
                )
            )
            if answers:
                break
    answers = list(dict.fromkeys(answers))
    if not answers:
        return HumanAnswerEvidence(
            "missing", "", "原子问题没有可核验且对应的人工实际答复；参考话术不能替代人工回答。",
        )
    # A final corrected decision supersedes a premature refusal in the same
    # exchange. Keep other factual steps, conditions and restrictions intact.
    if answers[-1].startswith(("判自动关机", "改判", "更正为")):
        answers = [answers[-1]]
    answer = "\n".join(answers)
    corrected = str(row.get("人工判定结论") or row.get("判定结论") or "")
    polarity_pairs = (
        (r"无需更换|不需要更换", r"必须更换|(?<!不)需要更换"),
        (r"不支持", r"(?<!不)支持(?:指纹|面容|回收|使用|该功能)"),
        (r"不可回收|无法回收|不能回收", r"可以回收|可正常回收"),
        (r"第三方更换|非原装|非原厂", r"(?<!非)原装|(?<!非)原厂"),
    )
    for negative, positive in polarity_pairs:
        if (
            re.search(negative, answer) and re.search(positive, corrected)
            or re.search(positive, answer) and re.search(negative, corrected)
        ):
            return HumanAnswerEvidence(
                "conflict", answer, "人工校正结论与原始人工回答存在相反结论，需要人工裁决。",
            )
    return HumanAnswerEvidence("answered", answer, "")


def source_answer_coverage_issues(content: str, rows: list[dict[str, Any]]) -> list[str]:
    """A brand from one member must not stand for a whole mixed topic."""
    if len(rows) < 2:
        return []
    brands = re.compile(
        r"三星|海力士|镁光|金士顿|华为|联想|戴尔|惠普|"
        r"micron|samsung|hynix|kingston", re.IGNORECASE,
    )
    required = {
        token.lower()
        for row in rows
        for token in brands.findall(str(row.get("_原子实际回复") or ""))
    }
    if len(required) < 2:
        return []
    missing = sorted(token for token in required if token not in content.lower())
    return ["多来源答案覆盖不足：" + "、".join(missing)] if missing else []


def has_any_human_answer(row: dict[str, Any]) -> bool:
    """Cheap source-level prefilter; ownership is checked after atom extraction."""
    if any(
        _substantive(fragment)
        for turn in _ROLE.finditer(str(row.get("聊天内容") or ""))
        if turn.group("role") in {"答疑", "答疑人员", "人工客服", "客服"}
        for fragment in split_answer_fragments(turn.group("text"))
    ):
        return True
    return any(
        _substantive(fragment)
        for field in ("实际回复", "答疑回复", "回复内容", "客服回复", "历史实际回复")
        if field != "历史实际回复" or row.get("历史实际回复来源类型") != "second_part_reference_reply"
        for fragment in split_answer_fragments(str(row.get(field) or ""))
    )
