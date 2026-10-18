from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_GENERIC_MARKERS = (
    "在设备设置的“关于本机/关于手机”中查看型号",
    "使用 IMEI、SN 或官方渠道核对出厂机型",
    "对照实物外观、功能配置和关键部件特征",
    "查询与实物不一致时，补充截图和实物照片后再判定",
    "确认异常出现于亮屏、白屏、黑屏、息屏或特定测试画面",
    "拍摄屏幕正面全景和异常点近景",
    "记录颜色、位置、数量、直径或面积并记录可复现的显示现象",
    "确认异常部位、材质及磕碰、划痕、磨损、掉漆、碎裂或脱胶类型",
    "拍摄整机全景、异常近景和侧视角度",
    "涉及尺寸或数量时补充量尺",
    "结合案例证据核对外观边界",
    "明确疑似拆修或维修痕迹的具体部位",
    "补充局部近景、整机全景和多角度照片",
    "核对原厂结构、胶痕、撬痕、部件标识和连接状态",
    "明确待核验功能、测试条件和所用配件",
    "排除电量、网络、权限、保护壳等外部影响",
    "使用一致的测试条件复测",
    "结果不稳定或无法复现时，补充测试证据后再判定",
    "明确待确认的对象、现象和对应问题",
    "补充支持判断的截图、照片、视频或查询结果",
    "结合案例证据确认适用条件、边界和例外",
)

_GENERIC_PATTERNS = (
    r"(?:明确|确认|先确认|核实|检查).{0,16}(?:对象|设备|现象|问题|功能|条件|配件)",
    r"(?:补充|提供|上传|记录|完善).{0,24}(?:截图|照片|视频|图片|查询结果|证据|资料|信息)",
    r"(?:结合|参考|根据).{0,16}(?:案例|证据).{0,24}(?:适用|范围|边界|例外|条件)",
    r"(?:无法|不能|暂时不能|不稳定|无法复现|无法明确).{0,24}(?:补充|完善).{0,16}(?:证据|资料|信息|测试).{0,16}(?:再|后).{0,8}(?:判定|处理|确认)",
    r"(?:排除|确认).{0,24}(?:电量|网络|权限|保护壳|外部影响)",
    r"(?:使用一致|统一|相同).{0,12}(?:测试条件|条件|流程|步骤).{0,12}(?:复测|测试|核验)",
    r"(?:在设备设置|使用\s*IMEI|官方渠道|关于本机|关于手机).{0,24}(?:查看|核对|确认).{0,12}(?:型号|机型|配置|信息)",
    r"(?:拍摄|补充).{0,24}(?:整机全景|异常近景|侧视角度|屏幕正面全景|局部近景|多角度照片)",
    r"(?:确认|记录).{0,24}(?:亮屏|白屏|黑屏|息屏|颜色|位置|数量|直径|面积|显示现象)",
    r"(?:确认|记录).{0,24}(?:异常部位|材质|磕碰|划痕|磨损|掉漆|碎裂|脱胶)",
    r"(?:核对|检查).{0,24}(?:原厂结构|胶痕|撬痕|部件标识|连接状态)",
)

_GENERIC_SOURCE_TERMS = frozenset(
    {
        "功能",
        "核验",
        "流程",
        "测试",
        "确认",
        "设备",
        "图片",
        "视频",
        "案例",
        "证据",
        "问题",
        "步骤",
        "对象",
        "现象",
        "条件",
        "结果",
        "信息",
        "影响",
        "补充",
        "判断",
        "处理",
        "来源",
        "待确认",
        "适用",
        "范围",
        "边界",
        "例外",
        "截图",
        "照片",
        "查询结果",
        "资料",
        "复测",
        "标准",
        "检测",
    }
)

_AUDIT_BOUNDARY_MARKERS = (
    "仅覆盖上述来源事实", "来源未说明的其他情形",
    "需要补充对应事实后再判断",
)


def strip_case_audit_boilerplate(content: str) -> str:
    """Keep audit restrictions out of the business answer shown to reviewers."""
    return "\n".join(
        "".join(
            segment for segment in re.split(r"(?<=[。；;])", line)
            if not any(marker in segment for marker in _AUDIT_BOUNDARY_MARKERS)
        )
        for line in str(content or "").splitlines()
    ).strip()


@dataclass(frozen=True)
class DraftQualityAssessment:
    decision: str
    reasons: tuple[str, ...]


def assess_case_only_draft(
    *,
    content: str,
    source_values: Iterable[str],
) -> DraftQualityAssessment:
    """Decide whether a case-only draft is too generic for automatic transcription."""

    normalized_content = _normalize(content)
    if not normalized_content:
        return DraftQualityAssessment(
            decision="manual_review",
            reasons=("正文为空，无法形成可追溯的案例知识。",),
        )
    answer_text = re.sub(r"(?m)^\s*\d+[.、]\s*", "", content).strip()
    if re.fullmatch(
        r"(?:您好[，,]?)?(?:请|建议|需要)?(?:补充|提供|拍摄|补拍)"
        r"(?:清晰|必要|相关|更多)?(?:图片|照片|截图|视频|信息|证据)"
        r"(?:后|再|以便)?(?:核验|确认|判断|判定|处理)?[。！!\s]*",
        answer_text,
    ):
        return DraftQualityAssessment(
            decision="manual_review",
            reasons=("正文只有补充资料的通用要求，尚无可沉淀的处理答案。",),
        )

    audit_content = any(
        marker in content
        for marker in (*_AUDIT_BOUNDARY_MARKERS, "回收师反馈", "关键事实：")
    )
    if audit_content:
        answer_segments = [
            segment for segment in re.split(r"[\n。；;]+", content)
            if not any(marker in segment for marker in _AUDIT_BOUNDARY_MARKERS)
            and not re.search(
                r"回收师反馈|用户反馈|关键事实[：:]|如何|怎么|什么|询问|[？?]",
                segment,
            )
        ]
        if not any(re.search(
            r"按.{0,24}(?:判|处理|选择|勾选)|判为|属于|归为|"
            r"请以.{0,60}为准|必须先|先.{0,24}再|时勾选|即为|"
            r"(?:应|需|建议|可以|可|不能|无需).{0,24}"
            r"(?:勾选|选择|填写|判定|处理|查询|查看|回收|核验)|"
            r"处理结论[：:]|处理方式[：:]",
            segment,
        ) or _has_source_specific_phrase(
            _normalize(segment),
            [
                value for value in source_values
                if not re.search(r"如何|怎么|什么|询问|[？?]", str(value or ""))
            ],
        )
            for segment in answer_segments):
            return DraftQualityAssessment(
                decision="manual_review",
                reasons=("正文只有问题摘要和审计说明，缺少来源支持的处理答案。",),
            )

    generic_hits = sum(marker in content for marker in _GENERIC_MARKERS)
    generic_hits += sum(
        bool(re.search(pattern, content)) for pattern in _GENERIC_PATTERNS
    )
    specific_content = re.sub(
        r"(?m)^(?:适用主题|适用情形)[：:].*$", "", content,
    )
    # Strip whole generic sentences first; partial marker removal can leave a
    # meaningless suffix that happens to match the copied source.
    specific_content = re.sub(
        r"(?:无法明确判断时|结果不稳定或无法复现时)[，,]?\s*"
        r"补充(?:测试)?证据后再判定",
        "",
        specific_content,
    )
    for marker in _GENERIC_MARKERS:
        specific_content = specific_content.replace(marker, "")
    # A copied question or generic step cannot establish a source-backed answer.
    specific_content = "。".join(
        segment
        for segment in re.split(r"[\n。；;]+", specific_content)
        if not re.search(r"如何|怎么|[？?]", segment)
    )
    if generic_hits < 3 or _has_source_specific_phrase(
        _normalize(specific_content),
        [
            sentence
            for source in source_values
            for sentence in re.split(r"[\n。；;]+", str(source or ""))
            if not any(re.search(pattern, sentence) for pattern in _GENERIC_PATTERNS)
            and not re.search(r"如何|怎么|[？?]|适用主题[：:]|适用情形[：:]", sentence)
        ],
    ):
        return DraftQualityAssessment(decision="pass", reasons=())
    return DraftQualityAssessment(
        decision="manual_review",
        reasons=("正文只有通用模板，未使用来源中的具体事实或结论。",),
    )


def has_source_specific_case_content(
    *,
    content: str,
    source_values: Iterable[str],
) -> bool:
    return _has_source_specific_phrase(_normalize(content), source_values)


def _has_source_specific_phrase(
    content: str,
    source_values: Iterable[str],
) -> bool:
    checked = 0
    for source in source_values:
        for segment in re.split(r"[\n，,。；;：:（）()“”\"'、]+", str(source or "")):
            normalized = _normalize(segment)
            if len(normalized) < 3:
                continue
            # A three or four-character overlap is often only the topic name
            # (for example, “转轴异响”), not a source-specific conclusion.
            for width in range(5, min(8, len(normalized)) + 1):
                for start in range(0, len(normalized) - width + 1):
                    phrase = normalized[start : start + width]
                    if phrase in _GENERIC_SOURCE_TERMS:
                        continue
                    if phrase in content:
                        return True
                    checked += 1
                    if checked >= 240:
                        return False
    return False


def _normalize(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))
