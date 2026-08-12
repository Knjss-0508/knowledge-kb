from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata

from .product_taxonomy import canonical_product_name


@dataclass(frozen=True)
class ClusteringJudgmentRule:
    rule_id: str
    product_categories: tuple[str, ...]
    standard_family: str
    merge_policy: str
    object_aliases: tuple[str, ...]
    phenomenon_values: tuple[tuple[str, tuple[str, ...]], ...]
    usage: str
    category_l1: str = ""


@dataclass(frozen=True)
class ClusteringRuleMatch:
    rule_id: str
    standard_family: str
    merge_policy: str
    phenomenon_value: str
    usage: str
    category_l1: str = ""
    category_l2: str = ""
    merge_boundary: str = ""
    exclusions_or_exceptions: str = ""
    detection_method_hint: str = ""


@dataclass(frozen=True)
class StandardFamilyIndexEntry:
    rule_id: str
    product_category: str
    category_l1: str
    category_l2: str
    standard_family: str
    merge_policy: str
    subject_aliases: tuple[str, ...]
    phenomenon_aliases: tuple[str, ...]
    merge_boundary: str
    decision_summary: str
    detection_method_hint: str
    exclusions_or_exceptions: str
    source_reference: str
    source_type: str


@dataclass(frozen=True)
class ClusteringFingerprint:
    product_category: str
    standard_family: str
    merge_policy: str
    object_key: str
    phenomenon_value: str
    query_target: str
    detection_target: str


_FINGERPRINT_REPLACEMENTS = (
    ("电池健康值", "电池健康度"),
    ("最大容量", "电池健康度"),
    ("查询机型", "型号查询"),
    ("查询型号", "型号查询"),
    ("设备型号", "型号查询"),
    ("具体型号", "型号查询"),
    ("型号确认", "型号查询"),
    ("机型确认", "型号查询"),
    ("机型核实", "型号查询"),
    ("型号核实", "型号查询"),
    ("小型号", "型号查询"),
    ("内存/硬盘", "内存硬盘"),
    ("存储/硬盘", "内存硬盘"),
    ("硬盘品牌", "内存硬盘品牌"),
    ("内存品牌", "内存硬盘品牌"),
    ("品牌硬盘", "内存硬盘品牌"),
    ("品牌内存", "内存硬盘品牌"),
    ("硬盘容量", "内存硬盘容量"),
    ("内存容量", "内存硬盘容量"),
    ("白光灯检测", "白光检测"),
    ("白光检查", "白光检测"),
    ("工具读数", "工具读取"),
    ("一根线", "工具读取"),
)


def _fingerprint_text(*values: object) -> str:
    text = unicodedata.normalize(
        "NFKC",
        " ".join(str(value or "") for value in values).casefold(),
    )
    for source, target in _FINGERPRINT_REPLACEMENTS:
        text = text.replace(source, target)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _fingerprint_query_target(
    text: str,
    *,
    product_category: str = "",
) -> str:
    product = _fingerprint_text(product_category)
    if product == _fingerprint_text("手机"):
        new_device_markers = (
            "全新机",
            "手机全新",
            "全新未拆封",
            "未拆包装",
            "三码合一",
            "塑封",
        )
        activation_anomaly_markers = (
            "激活日期异常",
            "激活时间异常",
            "1969",
            "1970",
        )
        if (
            any(marker in text for marker in new_device_markers)
            and not any(
                marker in text
                for marker in activation_anomaly_markers
            )
        ):
            return "new_device_eligibility"
        camera_damage_markers = (
            "磕碰",
            "掉漆",
            "碎裂",
            "裂纹",
            "划痕",
            "磨损",
        )
        if (
            any(marker in text for marker in ("摄像头", "镜头", "进光口"))
            and any(
                marker in text
                for marker in (
                    "印记",
                    "擦洗",
                    "异物",
                    "脏污",
                    "指印",
                    "泛白",
                )
            )
            and not any(marker in text for marker in camera_damage_markers)
        ):
            return "camera_lens_surface_condition"
        if (
            any(
                marker in text
                for marker in (
                    "外壳",
                    "后壳",
                    "中框",
                    "边框",
                    "机身",
                    "按键",
                    "摄像头区域",
                    "摄像头框",
                )
            )
            and any(
                marker in text
                for marker in (
                    "碎裂",
                    "裂纹",
                    "磕碰",
                    "磕点",
                    "凹陷",
                    "掉漆",
                    "缺角",
                    "玻璃磕点",
                )
            )
        ):
            return "phone_housing_appearance"
    if "白光检测" in text and any(
        marker in text for marker in ("方法", "操作", "角度", "怎么做", "如何做")
    ):
        return "detection_method"
    if "白光检测" in text and any(
        marker in text for marker in ("结果", "颜色异常", "底色", "光束", "正常不正常")
    ):
        return "detection_result"
    if "电池健康度" in text or "电池健康" in text:
        return "battery_health"
    if "国行" in text or "港澳台" in text or "版本查询" in text:
        return "device_version"
    if "序列号" in text or "imei" in text:
        if any(marker in text for marker in ("不一致", "不匹配", "对不上")):
            return "serial_mismatch"
        if any(marker in text for marker in ("查询失败", "读取失败", "乱码", "不正确")):
            return "serial_read_failure"
        if any(marker in text for marker in ("填写", "识别", "核实")):
            return "serial_identification"
        return "serial_query"
    if any(marker in text for marker in ("硬盘更换", "内存更换", "存储更换")):
        return "memory_storage_replacement"
    hardware_markers = (
        "内存",
        "运行内存",
        "硬盘",
        "固态硬盘",
        "ssd",
        "存储盘",
    )
    brand_markers = (
        "是否为品牌",
        "是不是品牌",
        "品牌属性",
        "品牌认证",
        "品牌件",
        "内存硬盘品牌",
        "原厂品牌",
        "非品牌",
        "第三方硬盘",
        "第三方内存",
        "非品牌",
        "牌子",
    )
    if (
        any(marker in text for marker in hardware_markers)
        and any(marker in text for marker in brand_markers)
    ):
        return "memory_storage_brand"
    if any(
        marker in text
        for marker in ("内存硬盘容量", "存储容量", "扩容")
    ):
        return "memory_storage_capacity"
    if any(
        marker in text
        for marker in (
            "型号查询",
            "型号版本",
            "第几款",
            "哪一款",
            "什么机型",
            "什么型号",
            "型号对不对",
            "机型对不对",
            "品牌机器",
            "什么品牌",
            "杂牌",
        )
    ):
        return "model_query"
    if "配置查询" in text or "配置核实" in text:
        return "device_configuration"
    return ""


def _fingerprint_detection_target(
    text: str,
    *,
    product_category: str = "",
) -> str:
    if "白光检测" in text:
        if any(marker in text for marker in ("方法", "操作", "角度", "怎么做")):
            return "method"
        return "result"
    if any(marker in text for marker in ("偏光检测", "红线检测")):
        return "method"
    if (
        _fingerprint_text(product_category)
        in {
            _fingerprint_text("平板"),
            _fingerprint_text("笔记本"),
        }
        and "屏幕" in text
        and any(
            marker in text
            for marker in (
                "老化",
                "泛红",
                "发红",
                "泛黄",
                "发黄",
                "整体偏色",
                "色偏",
            )
        )
    ):
        return "screen_color_aging"
    if (
        _fingerprint_text(product_category) == _fingerprint_text("笔记本")
        and "屏幕" in text
        and "色斑" in text
        and not any(
            marker in text
            for marker in (
                "老化",
                "泛红",
                "泛黄",
                "整体偏色",
                "横纹",
                "条纹",
            )
        )
    ):
        return "screen_color_spot"
    return ""


def _phone_new_device_context_target(
    primary: str,
    context: str,
    *,
    product_category: str,
) -> str:
    if _fingerprint_text(product_category) != _fingerprint_text("手机"):
        return ""
    boundary_markers = (
        "包装盒防拆标签",
        "防拆标签",
        "防拆标",
        "未激活",
        "监管信息",
        "新机包装",
    )
    activation_anomaly_markers = (
        "激活日期异常",
        "激活时间异常",
        "1969",
        "1970",
    )
    combined = f"{primary}{context}"
    if (
        not any(marker in primary for marker in boundary_markers)
        or any(marker in combined for marker in activation_anomaly_markers)
    ):
        return ""
    context_target = _fingerprint_query_target(
        context,
        product_category=product_category,
    )
    return (
        context_target
        if context_target == "new_device_eligibility"
        else ""
    )


def _fingerprint_object_key(subject: object, rule: ClusteringRuleMatch | None) -> str:
    subject_text = _fingerprint_text(subject)
    if not subject_text:
        return ""
    # Keep concrete sub-parts separate even when they belong to one broad
    # appearance family. This prevents camera frames, housings and lenses
    # from being merged only because the standard family is similar.
    for marker in (
        "后置摄像头",
        "前置摄像头",
        "摄像头",
        "镜片",
        "镜头",
        "后壳",
        "中框",
        "边框",
        "外壳",
        "屏幕",
        "硬盘",
        "固态硬盘",
        "运行内存",
        "内存",
        "设备型号",
        "型号",
        "a面",
        "b面",
        "c面",
        "d面",
        "电池",
        "主板",
        "螺丝",
    ):
        if marker in subject_text:
            return marker
    if rule is not None:
        return _fingerprint_text(rule.standard_family)
    return subject_text


def build_clustering_fingerprint(
    *,
    product_category: str,
    category_l1: str = "",
    intent: str = "",
    subject: str = "",
    phenomenon: str = "",
    normalized_issue: str = "",
    judgment_target: str = "",
    resolution_mode: str = "",
    standard_path: str = "",
    conversation: str = "",
) -> ClusteringFingerprint:
    rule = match_clustering_judgment_rule(
        product_category=product_category,
        subject=subject,
        phenomenon=phenomenon,
        normalized_issue=normalized_issue,
        conversation=conversation,
    )
    primary = _fingerprint_text(
        subject,
        phenomenon,
        normalized_issue,
        judgment_target,
        resolution_mode,
        standard_path,
    )
    context = _fingerprint_text(
        category_l1,
        intent,
        conversation,
    )
    query_target = _fingerprint_query_target(
        primary,
        product_category=product_category,
    )
    if not query_target:
        query_target = _phone_new_device_context_target(
            primary,
            context,
            product_category=product_category,
        )
    detection_target = _fingerprint_detection_target(
        primary,
        product_category=product_category,
    )
    if not query_target and len(primary) < 12:
        query_target = _fingerprint_query_target(
            context,
            product_category=product_category,
        )
    if not detection_target and len(primary) < 12:
        detection_target = _fingerprint_detection_target(
            context,
            product_category=product_category,
        )
    return ClusteringFingerprint(
        product_category=_fingerprint_text(product_category),
        standard_family=_fingerprint_text(rule.standard_family if rule else ""),
        merge_policy=_fingerprint_text(rule.merge_policy if rule else ""),
        object_key=_fingerprint_object_key(subject, rule),
        phenomenon_value=_fingerprint_text(
            rule.phenomenon_value if rule else phenomenon
        ),
        query_target=query_target,
        detection_target=detection_target,
    )


CLUSTERING_JUDGMENT_RULES: tuple[ClusteringJudgmentRule, ...] = (
    ClusteringJudgmentRule(
        rule_id="phone-housing-appearance",
        product_categories=("手机",),
        standard_family="手机外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("外壳", "后壳", "中框", "边框", "机身外观", "壳体"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂", "破裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕", "缺角")),
            ("掉漆", ("掉漆", "掉色", "漆面脱落")),
            ("磨损", ("磨损", "油光")),
            ("划痕", ("划痕", "刮痕")),
            ("弯曲变形", ("弯曲", "变形")),
        ),
        usage=(
            "碎裂、磕碰、掉漆、磨损是同一外壳外观标准族下的不同现象值；"
            "对象均为手机外壳且判定目标一致时，可作为同主题候选。"
        ),
    ),
    ClusteringJudgmentRule(
        rule_id="phone-screen-display",
        product_categories=("手机",),
        standard_family="手机屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "显示屏", "屏幕显示", "液晶"),
        phenomenon_values=(
            ("漏液", ("漏液", "液晶漏液")),
            ("色斑", ("色斑", "色块", "斑块")),
            ("亮斑", ("亮斑",)),
            ("坏点", ("坏点", "死点", "坏像素", "黑点")),
            ("条纹", ("条纹", "屏线", "亮线", "彩线", "线条")),
            ("闪烁", ("闪烁", "闪屏")),
            ("图层错乱", ("图层错乱", "花屏")),
            ("图文残影", ("图文残影", "残影", "透图", "图像残留")),
            ("漏光", ("漏光", "透光")),
            ("波纹", ("波纹",)),
            ("局部发暗", ("发暗", "暗角")),
            ("色偏老化", ("泛黄", "泛红", "泛灰", "发黄", "发红")),
        ),
        usage=(
            "漏液、色斑、坏点、条纹分别对应不同的屏幕显示现象值；"
            "即使都属于屏幕问题，不同现象值必须拆分。"
        ),
    ),
    ClusteringJudgmentRule(
        rule_id="tablet-screen-display",
        product_categories=("平板",),
        standard_family="平板屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "显示屏", "屏幕显示", "液晶"),
        phenomenon_values=(
            ("漏液", ("漏液", "液晶漏液")),
            ("色斑", ("色斑", "色块", "斑块")),
            ("亮斑", ("亮斑",)),
            ("坏点", ("坏点", "死点", "坏像素", "黑点")),
            ("条纹", ("条纹", "屏线", "亮线", "彩线", "线条")),
            ("闪烁", ("闪烁", "闪屏")),
            ("图层错乱", ("图层错乱", "花屏")),
            ("图文残影", ("图文残影", "残影", "透图", "图像残留")),
            ("漏光", ("漏光", "透光")),
            ("波纹", ("波纹",)),
            ("局部发暗", ("发暗", "暗角")),
            ("色偏老化", ("泛黄", "泛红", "泛灰", "发黄", "发红")),
        ),
        usage="平板屏幕显示异常按漏液、色斑、坏点、条纹等现象值拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-shell-appearance",
        product_categories=("笔记本",),
        standard_family="笔记本外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("外壳", "后壳", "底壳", "掌托"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂", "破裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕", "缺角")),
            ("掉漆", ("掉漆", "掉色", "漆面脱落")),
            ("磨损", ("磨损", "油光")),
            ("划痕", ("划痕", "刮痕")),
            ("弯曲变形", ("弯曲", "变形")),
        ),
        usage="同一笔记本外壳部位的外观损伤可按同一外观标准族判断。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-screen-display",
        product_categories=("笔记本",),
        standard_family="笔记本屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("B面", "屏幕", "显示屏", "液晶"),
        phenomenon_values=(
            ("漏液", ("漏液", "液晶漏液")),
            ("色斑", ("色斑", "色块", "斑块")),
            ("亮斑", ("亮斑",)),
            ("坏点", ("坏点", "死点", "坏像素", "黑点")),
            ("条纹", ("条纹", "屏线", "亮线", "花屏", "闪屏")),
            ("图文残影", ("图文残影", "残影", "透图", "图像残留")),
            ("漏光", ("漏光", "透光")),
            ("色偏老化", ("泛黄", "泛红", "发黄", "发红")),
        ),
        usage="笔记本屏幕显示问题按具体显示现象值拆分，不与外壳外观问题合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-internal",
        product_categories=("相机镜头",),
        standard_family="镜头内部光学状态标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("镜头", "镜片", "镜内", "光学镜片"),
        phenomenon_values=(
            ("进灰", ("进灰", "灰尘", "灰粒")),
            ("异物", ("异物", "绒毛", "金属屑", "虫尸")),
            ("发霉", ("发霉", "霉斑", "霉菌")),
        ),
        usage="镜头进灰、异物、发霉的成因和判定特征不同，必须按现象值拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="stylus-components",
        product_categories=("手写笔",),
        standard_family="手写笔部件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("手写笔", "笔尖", "笔芯", "笔帽", "笔头"),
        phenomenon_values=(
            ("笔尖", ("笔尖", "笔头", "替换笔尖")),
            ("笔芯", ("笔芯",)),
            ("笔帽", ("笔帽", "笔尾", "笔帽钢圈")),
        ),
        usage="笔尖、笔芯、笔帽是不同部件，不得因都属于手写笔而合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="learning-device-touch",
        product_categories=("学习机",),
        standard_family="学习机触控异常标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "触控", "触摸"),
        phenomenon_values=(
            ("触控失灵", ("断触", "触控断点", "失灵", "触控失灵")),
            ("乱触", ("乱触", "鬼触")),
            ("漂移", ("漂移", "触摸偏移")),
        ),
        usage="断触、乱触、漂移的表现和检测方式不同，必须按现象值拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="phone-screen-surface",
        product_categories=("手机",),
        standard_family="手机屏幕外观标准",
        merge_policy="same_standard_family",
        object_aliases=("屏幕", "外屏", "内屏", "玻璃盖板", "屏幕支架"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕点", ("磕点", "磕碰", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("气泡", ("气泡",)),
        ),
        usage=(
            "手机屏幕碎裂、磕点、掉漆、气泡属于同一屏幕外观标准族；"
            "仅可作为候选，仍须由聊天中的具体尺度和处理结论决定是否合并。"
        ),
    ),
    ClusteringJudgmentRule(
        rule_id="phone-touch",
        product_categories=("手机",),
        standard_family="手机触控标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("触控", "触屏", "屏幕触控", "副屏触控"),
        phenomenon_values=(
            ("触控失灵", ("失灵", "断触", "触控断点")),
            ("乱触", ("乱触", "鬼触")),
            ("漂移", ("漂移", "触摸偏移")),
            ("3D Touch无反馈", ("3d touch", "按压无反馈")),
        ),
        usage="触控失灵、乱触、漂移和3D Touch无反馈属于不同触控现象，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="phone-front-camera-function",
        product_categories=("手机",),
        standard_family="手机前置摄像头功能标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("前摄", "前置摄像头", "自拍摄像头", "升降摄像头"),
        phenomenon_values=(
            ("无画面或报错", ("无画面", "闪退", "报错", "无法切换", "卡顿")),
            ("成像异常", ("模糊", "抖动", "不对焦", "画面异常")),
            ("成像有斑", ("有斑", "照片有斑", "画面有斑")),
            ("成像坏点", ("坏点", "坏像素")),
            ("升降异常", ("无法升降", "升降异常")),
        ),
        usage="前摄无画面、成像异常、成像有斑、坏点和升降异常分别对应不同判定口径。",
    ),
    ClusteringJudgmentRule(
        rule_id="phone-rear-camera-function",
        product_categories=("手机",),
        standard_family="手机后置摄像头功能标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("后摄", "后置摄像头", "主摄", "主摄像头"),
        phenomenon_values=(
            ("无画面或报错", ("无画面", "闪退", "报错", "无法切换", "卡顿")),
            ("成像异常", ("模糊", "抖动", "不对焦", "画面异常")),
            ("成像有斑", ("有斑", "照片有斑", "画面有斑")),
            ("成像坏点", ("坏点", "坏像素")),
        ),
        usage="后摄问题不得与前摄、屏幕或镜头外观问题合并；同一后摄内按现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="phone-repair-component",
        product_categories=("手机",),
        standard_family="手机拆修部件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=(
            "前摄",
            "前置摄像头",
            "后摄",
            "后置摄像头",
            "电池",
            "屏幕",
            "后壳",
            "主板",
        ),
        phenomenon_values=(
            ("前摄拆修", ("前摄拆修", "前摄维修", "前摄更换", "第三方前摄")),
            ("后摄拆修", ("后摄拆修", "后摄维修", "后摄更换", "第三方后摄")),
            ("电池拆修", ("电池拆修", "电池维修", "电池更换", "第三方电池", "外挂小板")),
            ("屏幕拆修", ("屏幕拆修", "非原厂屏", "整屏焕新", "外屏焕新", "更换外玻璃")),
            ("后壳拆修", ("后壳拆修", "后壳维修", "后壳更换", "第三方后壳")),
            ("主板拆修", ("主板拆修", "主板维修", "主板焊接", "主板飞线")),
        ),
        usage="手机拆修必须按前摄、后摄、电池、屏幕、后壳、主板等具体部件拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="tablet-housing-appearance",
        product_categories=("平板",),
        standard_family="平板外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("外壳", "后壳", "中框", "边框", "机身外观"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕", "缺角")),
            ("掉漆", ("掉漆", "掉色")),
            ("磨损", ("磨损", "油光", "氧化", "脏污")),
            ("划痕", ("划痕", "刮痕")),
            ("弯曲变形", ("弯曲", "变形")),
        ),
        usage="平板外壳的碎裂、磕碰、掉漆、磨损、划痕和弯曲属于外壳外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="tablet-screen-surface",
        product_categories=("平板",),
        standard_family="平板屏幕外观标准",
        merge_policy="same_standard_family",
        object_aliases=("屏幕", "外屏", "玻璃盖板", "显示屏"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕点", ("磕点", "磕碰", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("气泡", ("气泡",)),
        ),
        usage="平板屏幕玻璃的碎裂、磕点、掉漆和气泡属于屏幕外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="tablet-touch",
        product_categories=("平板",),
        standard_family="平板触控标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("触控", "触屏", "屏幕触控"),
        phenomenon_values=(
            ("触控失灵", ("失灵", "断触", "触控断点")),
            ("乱触", ("乱触", "鬼触")),
            ("漂移", ("漂移", "触摸偏移")),
        ),
        usage="平板触控失灵、乱触和漂移必须按具体现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="stylus-use-condition",
        product_categories=("手写笔",),
        standard_family="手写笔使用状态标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("手写笔", "触控笔", "笔"),
        phenomenon_values=(
            ("定位账号绑定", ("定位功能异常", "查找定位", "无法解绑", "定位账号")),
            ("配对或断连", ("无法配对", "无法连接", "断连", "自动断连")),
            ("无法书写", ("无法书写", "书写断续", "书写无响应")),
            ("序列号异常", ("序列号异常", "序列号更换")),
        ),
        usage="账号定位绑定、配对断连、无法书写和序列号异常的处理路径不同，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="stylus-function",
        product_categories=("手写笔",),
        standard_family="手写笔功能标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("手写笔", "按键", "笔身", "压感"),
        phenomenon_values=(
            ("按键异常", ("按键失灵", "弹性弱", "按键异常")),
            ("双击切换异常", ("双击无法切换", "双击切换异常")),
            ("压感失灵", ("压感失灵", "压力感应失灵")),
        ),
        usage="按键、双击切换和压感是不同功能项，不能因都属于手写笔功能而合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="stylus-housing-appearance",
        product_categories=("手写笔",),
        standard_family="手写笔外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("笔身", "外壳", "笔帽", "笔杆"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "破损")),
            ("磕碰", ("磕碰", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("变形", ("变形", "弯曲")),
            ("磨损", ("磨损", "氧化", "脏污")),
            ("划痕", ("划痕", "刮痕")),
        ),
        usage="手写笔笔身外观损伤可作为同一外观标准族候选；笔尖、笔芯拆修仍必须分开。",
    ),
    ClusteringJudgmentRule(
        rule_id="learning-device-screen-display",
        product_categories=("学习机",),
        standard_family="学习机屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "显示屏", "液晶"),
        phenomenon_values=(
            ("漏液", ("漏液", "液晶漏液")),
            ("条纹", ("条纹", "屏线", "亮线")),
            ("闪烁", ("闪烁", "闪屏")),
            ("图层错乱", ("图层错乱", "花屏")),
            ("漏光", ("透光", "漏光")),
            ("波纹", ("波纹",)),
            ("局部发暗", ("发暗",)),
            ("色偏老化", ("泛黄", "泛红", "发黄", "发红")),
            ("亮斑", ("亮斑",)),
            ("坏点", ("坏点", "死点", "坏像素")),
            ("色斑", ("色斑", "色块")),
            ("图文残影", ("透图", "残影", "图文残影")),
        ),
        usage="学习机屏幕显示问题必须按漏液、条纹、闪烁、亮斑、坏点、色斑、透图等现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="learning-device-housing-appearance",
        product_categories=("学习机",),
        standard_family="学习机外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("外壳", "后壳", "中框", "边框", "机身外观"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("磨损", ("磨损", "氧化", "脏污", "油光")),
            ("划痕", ("划痕", "刮痕")),
            ("弯曲变形", ("弯曲", "变形")),
        ),
        usage="学习机外壳外观损伤可作为同一标准族候选，不与屏幕或拆修问题合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="learning-device-repair",
        product_categories=("学习机",),
        standard_family="学习机拆修标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "螺丝", "外壳", "机身"),
        phenomenon_values=(
            ("屏幕拆修", ("屏幕溢胶", "屏内划痕", "屏内印记", "屏内异物")),
            ("螺丝拆修", ("螺丝拧动", "螺丝缺失")),
            ("外壳拆修", ("外壳撬痕", "外壳溢胶")),
        ),
        usage="学习机的屏幕、螺丝和外壳拆修痕迹是不同质检项，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-a-surface-appearance",
        product_categories=("笔记本",),
        standard_family="笔记本A面外观标准",
        merge_policy="same_standard_family",
        object_aliases=("A面", "上盖", "屏幕后盖", "屏幕背板"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("磨损", ("磨损", "油光")),
            ("划痕", ("划痕", "刮痕")),
        ),
        usage="笔记本A面内的外观损伤可作为同一标准族候选，不与C面、D面混合。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-c-surface-appearance",
        product_categories=("笔记本",),
        standard_family="笔记本C面外观标准",
        merge_policy="same_standard_family",
        object_aliases=("C面", "键盘面", "掌托", "触控板"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("磨损", ("磨损", "油光")),
            ("划痕", ("划痕", "刮痕")),
        ),
        usage="笔记本C面外观与A面、D面是不同部位，不能跨面合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-d-surface-appearance",
        product_categories=("笔记本",),
        standard_family="笔记本D面外观标准",
        merge_policy="same_standard_family",
        object_aliases=("D面", "底壳", "底盖"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("磨损", ("磨损", "油光")),
            ("划痕", ("划痕", "刮痕")),
        ),
        usage="笔记本D面外观与A面、C面是不同部位，不能跨面合并。",
    ),
    ClusteringJudgmentRule(
        rule_id="notebook-camera",
        product_categories=("笔记本",),
        standard_family="笔记本摄像头标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("摄像头", "摄像头镜片", "摄像头开关", "摄像头物理开关"),
        phenomenon_values=(
            ("镜片或灯罩损伤", ("镜片碎裂", "灯罩碎裂", "镜片破损")),
            ("物理开关异常", ("开关失灵", "物理开关异常")),
            ("成像异常", ("泛红", "泛蓝", "模糊", "雪花", "颗粒", "画面异常")),
        ),
        usage="笔记本摄像头外观、物理开关和成像异常是不同标准，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-control-rings",
        product_categories=("相机镜头",),
        standard_family="镜头操作环标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("变焦环", "对焦环", "光圈环", "镜头操作环"),
        phenomenon_values=(
            ("变焦环异常", ("变焦环失灵", "变焦环卡顿")),
            ("对焦环异常", ("对焦环失灵", "对焦环卡顿")),
            ("光圈环异常", ("光圈环失灵", "光圈环卡顿")),
        ),
        usage="变焦环、对焦环和光圈环控制目标不同，发生卡顿或失灵时必须分别聚类。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-imaging",
        product_categories=("相机镜头",),
        standard_family="镜头成像功能标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("镜头", "成像", "视频", "对焦", "光圈"),
        phenomenon_values=(
            ("彩斑纹路或卡顿", ("彩斑", "纹路", "卡顿", "抖动")),
            ("对焦异常", ("对焦模糊", "对焦偏移", "无法对焦")),
            ("成像有斑", ("成像有斑", "照片有斑", "画面有斑")),
            ("光圈异常", ("光圈无法调节", "光圈失效")),
        ),
        usage="镜头的成像、对焦、光圈问题有独立检测和处理路径，必须按现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-housing-appearance",
        product_categories=("相机镜头",),
        standard_family="镜头外壳外观标准",
        merge_policy="same_standard_family",
        object_aliases=("镜头外壳", "镜头筒", "镜身", "镜头"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("划痕", ("划痕", "刮痕")),
            ("污渍", ("污渍", "脏污")),
            ("油光", ("油光",)),
        ),
        usage="镜头外壳的碎裂、磕碰、掉漆、划痕、污渍和油光可作为同一外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-glass-surface",
        product_categories=("相机镜头",),
        standard_family="镜片外观标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("镜片", "前镜片", "后镜片", "光学镜片"),
        phenomenon_values=(
            ("磕点或碎裂", ("磕点", "碎裂", "裂纹")),
            ("镜片划痕", ("划痕", "刮痕")),
            ("镀膜脱落", ("镀膜脱落", "光学镀膜脱落")),
            ("消光漆脱落", ("消光漆脱落", "内部掉漆")),
        ),
        usage="镜片磕点碎裂、划痕、镀膜脱落和消光漆脱落为不同镜片标准，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-lens-repair-and-liquid",
        product_categories=("相机镜头",),
        standard_family="镜头拆修与浸液标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("镜头", "螺丝", "外壳", "内部"),
        phenomenon_values=(
            ("螺丝或部件拆修", ("螺丝缺失", "螺丝更换", "螺口磨损", "第三方螺丝", "维修痕迹")),
            ("改版机", ("改版机", "型号改写")),
            ("明显补漆", ("明显补漆",)),
            ("轻微补漆", ("轻微补漆",)),
            ("内部浸液", ("内部浸液", "内部浸液痕迹")),
            ("外部浸液", ("外部浸液", "外部浸液痕迹")),
        ),
        usage="镜头螺丝拆修、改版、补漆和内外浸液是不同标准路径，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-body-screen-display",
        product_categories=("相机机身",),
        standard_family="相机机身屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("主屏", "副屏", "屏幕", "显示屏"),
        phenomenon_values=(
            ("漏液", ("漏液",)),
            ("条纹", ("条纹", "屏线")),
            ("闪烁", ("闪烁", "闪屏")),
            ("色斑", ("色斑", "色块")),
            ("亮斑", ("亮斑",)),
            ("坏点", ("亮点", "坏点", "死点")),
            ("暗角", ("暗角",)),
            ("色偏老化", ("泛黄", "泛红", "泛灰", "发黄", "发红")),
        ),
        usage="相机机身主副屏的漏液、条纹、闪烁、色斑、亮斑、坏点和老化必须按现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-body-imaging",
        product_categories=("相机机身",),
        standard_family="相机机身成像标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("成像", "视频", "取景器", "对焦", "相机机身"),
        phenomenon_values=(
            ("彩斑纹路或视频异常", ("彩斑", "纹路", "视频异常", "视频无声")),
            ("成像有斑", ("成像有斑", "照片有斑", "画面有斑")),
            ("成像坏点", ("成像坏点", "成像死点")),
            ("取景器异常", ("取景器显示异常", "取景器无法使用")),
            ("无法对焦", ("无法对焦", "对焦异常")),
        ),
        usage="机身成像、取景器和对焦问题是独立质检项，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-body-housing-appearance",
        product_categories=("相机机身",),
        standard_family="相机机身外观标准",
        merge_policy="same_standard_family",
        object_aliases=("机身", "外壳", "机身外观", "壳体"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "开裂")),
            ("磕碰", ("磕碰", "磕伤", "凹痕")),
            ("掉漆", ("掉漆", "掉色")),
            ("划痕", ("划痕", "刮痕")),
            ("污渍", ("污渍", "脏污")),
            ("油光", ("油光",)),
        ),
        usage="相机机身外壳的碎裂、磕碰、掉漆、划痕、污渍和油光可作为同一外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-body-sensitive-components",
        product_categories=("相机机身",),
        standard_family="相机机身关键部件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("取景器", "CMOS", "CCD", "感光元件", "低通滤镜", "热靴", "存储卡", "数据接口"),
        phenomenon_values=(
            ("取景器外观", ("眼罩缺失", "取景器镜片碎裂", "取景器进灰", "取景器发霉")),
            ("传感器外观", ("cmos", "ccd", "感光元件", "低通滤镜")),
            ("热靴接口异常", ("热靴失灵", "热靴损坏")),
            ("存储卡异常", ("无法读取", "无法插取", "存储卡报错")),
            ("数据接口异常", ("数据接口失灵", "数据接口损坏")),
        ),
        usage="取景器、传感器、热靴、存储卡和数据接口是不同部件，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="camera-body-repair-and-liquid",
        product_categories=("相机机身",),
        standard_family="相机机身拆修与浸液标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("电池", "屏幕", "螺丝", "外壳", "机身", "对焦屏"),
        phenomenon_values=(
            ("电池拆修", ("电池鼓包", "仿冒电池", "非品牌电池", "品牌电池", "电池效率低")),
            ("屏幕拆修", ("屏幕拆修", "屏幕维修")),
            ("螺丝或部件拆修", ("螺丝缺失", "螺丝更换", "螺口磨损", "第三方螺丝", "维修痕迹")),
            ("对焦屏异常", ("对焦屏异常", "对焦屏缺失")),
            ("改版机", ("改版机", "型号改写")),
            ("明显补漆", ("明显补漆",)),
            ("轻微补漆", ("轻微补漆",)),
            ("内部浸液", ("内部浸液", "内部浸液痕迹")),
            ("外部浸液", ("外部浸液", "外部浸液痕迹")),
        ),
        usage="机身电池、屏幕、螺丝、对焦屏、改版补漆和内外浸液是不同标准，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="game-cartridge-use-status",
        product_categories=("游戏卡带",),
        standard_family="游戏卡带使用状态标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("卡带", "游戏卡", "游戏卡带"),
        phenomenon_values=(
            ("外壳破损", ("外壳破损", "卡带破损")),
            ("无法使用", ("无法使用", "无法读取", "无法识别")),
        ),
        usage="游戏卡带外壳破损与无法使用分别属于外观和功能问题，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="game-cartridge-packaging",
        product_categories=("游戏卡带",),
        standard_family="游戏卡带包装标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("包装盒", "包装", "箱说", "说明书"),
        phenomenon_values=(
            ("包装盒破损缺失或不匹配", ("包装盒破损", "包装盒缺失", "包装盒不匹配")),
            ("箱说齐全", ("箱说齐全",)),
            ("有包装", ("有包装",)),
            ("无包装", ("无包装",)),
        ),
        usage="包装盒外观、箱说齐全和有无包装是不同包装查验结论，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="game-cartridge-accessory",
        product_categories=("游戏卡带",),
        standard_family="游戏卡带配件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("健身环", "配件", "卡带配件"),
        phenomenon_values=(
            ("健身环异常", ("健身环缺失", "健身环无法使用")),
            ("配件不齐全", ("配件不齐全",)),
            ("出厂无配件", ("官方出厂无配件",)),
        ),
        usage="健身环和普通配件清单是不同检测对象，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-functional-components",
        product_categories=("耳机",),
        standard_family="耳机功能部件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=(
            "耳机",
            "听筒",
            "麦克风",
            "降噪",
            "通透",
            "入耳感应",
            "按键",
            "触控",
            "充电",
            "充电盒",
        ),
        phenomenon_values=(
            ("听筒异常", ("听筒无声", "听筒杂音", "听筒声音小")),
            ("麦克风异常", ("送话无声", "送话弱", "麦克风杂音")),
            ("降噪异常", ("无法开启降噪", "无降噪效果")),
            ("通透异常", ("无法切换通透", "无通透感")),
            ("入耳感应异常", ("入耳感应失效",)),
            ("按键异常", ("按键失灵", "按键缺失", "弹性差", "按键凹陷")),
            ("触控异常", ("触控无反应", "触控间歇性失灵")),
            ("充电异常", ("充电断续", "充电盒灯不亮", "无法充电")),
            ("充电盒异常", ("充电盒开合卡顿", "磁吸缺失", "屏幕显示异常")),
        ),
        usage="耳机听筒、麦克风、降噪、通透、入耳感应、按键、触控、充电及充电盒是不同功能标准。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-use-and-connection",
        product_categories=("耳机",),
        standard_family="耳机使用与账号标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("耳机", "单耳", "充电盒", "查找功能", "定位"),
        phenomenon_values=(
            ("无法连接或配对", ("无法连接", "无法配对", "配对失败")),
            ("查找账号绑定", ("查找功能已绑定账户", "定位功能异常", "账号未解绑")),
            ("缺失单耳", ("缺失单耳机", "单耳缺失")),
            ("仿冒或序列号异常", ("仿冒产品", "序列号异常", "字体重塑")),
        ),
        usage="连接配对、查找账号、单耳缺失及仿冒序列号是不同使用状态，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-body-appearance",
        product_categories=("耳机",),
        standard_family="耳机本体外观标准",
        merge_policy="same_standard_family",
        object_aliases=("耳机机身", "耳机外壳", "耳机本体", "机身"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "破损")),
            ("磕碰掉漆磨损", ("磕碰", "掉漆", "磨损")),
            ("划痕", ("划痕", "刮痕")),
            ("变形或缝隙", ("变形", "缝隙", "脱胶")),
            ("进灰", ("进灰",)),
        ),
        usage="耳机本体碎裂、磕碰、掉漆、磨损、划痕和轻微变形可作为本体外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-case-appearance",
        product_categories=("耳机",),
        standard_family="耳机充电盒外观标准",
        merge_policy="same_standard_family",
        object_aliases=("充电盒", "耳机盒", "盒身"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹", "破损")),
            ("磕碰掉漆磨损", ("磕碰", "掉漆", "磨损")),
            ("划痕", ("划痕", "刮痕")),
            ("弯曲或缝隙", ("弯曲", "缝隙", "脱胶")),
            ("进灰", ("进灰",)),
        ),
        usage="充电盒外观与耳机本体外观是不同对象，盒身现象可在本标准族内作为候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-accessory-appearance",
        product_categories=("耳机",),
        standard_family="耳机耳帽耳套与线材标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("耳帽", "耳套", "耳线", "头梁", "防尘网"),
        phenomenon_values=(
            ("耳帽或耳套破损", ("耳帽破损", "耳套破损")),
            ("耳帽或耳套脏污油光变色", ("油光", "污渍", "变色")),
            ("耳线破损", ("耳线破损",)),
            ("头梁异常", ("头梁松弛", "头梁变形", "头梁网纱缺失")),
            ("防尘网异常", ("防尘网缺失", "防尘网破损", "防尘网变形")),
        ),
        usage="耳帽、耳套、耳线、头梁和防尘网是不同部件，必须按部件和现象拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="headphones-repair-and-liquid",
        product_categories=("耳机",),
        standard_family="耳机拆修与浸液标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("耳机", "充电盒", "电池", "接口", "防尘网"),
        phenomenon_values=(
            ("部件更换", ("更换原装左耳", "更换原装右耳", "部件更换")),
            ("平台焕新", ("平台焕新", "兼容耳帽", "兼容耳套", "兼容电池")),
            ("撬痕溢胶或卡扣异常", ("撬痕", "溢胶", "卡扣变形")),
            ("螺丝拆修", ("螺丝不匹配", "螺丝拧动", "螺丝缺失")),
            ("充电接口或防尘网更换", ("充电接口更换", "防尘网更换")),
            ("浸液或生锈", ("发霉", "水渍", "生锈", "防水标变红")),
        ),
        usage="耳机更换、焕新、撬痕、螺丝、接口及浸液属于不同拆修路径，必须拆分。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-screen-display",
        product_categories=("手表",),
        standard_family="手表屏幕显示标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "内屏", "显示屏"),
        phenomenon_values=(
            ("漏液条纹闪屏", ("漏液", "条纹", "闪屏", "闪烁")),
            ("色斑亮斑", ("色斑", "亮斑")),
            ("亮点坏点", ("亮点", "坏点")),
            ("透图残影", ("透图", "残影")),
            ("色偏老化", ("发黄", "发红", "老化")),
        ),
        usage="手表漏液、条纹、闪屏、色斑、亮点坏点、透图和老化是不同显示现象。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-screen-appearance",
        product_categories=("手表",),
        standard_family="手表屏幕外观标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "内屏", "外玻璃", "屏幕外观"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹")),
            ("磕点", ("磕点", "凹痕")),
            ("有触感划痕", ("有触感划痕", "有触感-划痕")),
            ("无触感划痕", ("无触感划痕", "无触感-划痕")),
            ("褶皱", ("褶皱", "鼓包", "凸点", "印记")),
            ("进灰或掉漆气泡", ("进灰", "掉漆", "气泡", "疏油层")),
        ),
        usage="手表屏幕碎裂、磕点、有无触感划痕、褶皱及进灰气泡为不同外观口径。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-body-and-band-appearance",
        product_categories=("手表",),
        standard_family="手表机身外观标准",
        merge_policy="same_standard_family",
        object_aliases=("机身", "外壳", "表圈", "表体"),
        phenomenon_values=(
            ("碎裂", ("碎裂", "裂纹")),
            ("磕碰掉漆", ("磕碰", "掉漆")),
            ("划痕", ("划痕", "刮痕")),
            ("脱胶或缝隙", ("脱胶", "缝隙")),
            ("防尘网或卡扣异常", ("防尘网", "卡扣")),
        ),
        usage="手表机身的碎裂、磕碰、掉漆、划痕和结构缝隙可作为机身外观标准族候选。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-band-appearance",
        product_categories=("手表",),
        standard_family="手表表带标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("表带", "表链"),
        phenomenon_values=(
            ("表带缺失或不匹配", ("表带碎裂", "表带缺失", "颜色不一致", "大小不一致", "少节")),
            ("表带磨损或磕碰", ("表带磨损", "表带掉漆", "折痕", "表带划痕", "表带磕碰")),
        ),
        usage="表带与表体是不同对象，表带缺失匹配和表带磨损磕碰也应分开。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-functional-components",
        product_categories=("手表",),
        standard_family="手表功能部件标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=(
            "按键",
            "触控",
            "SIM",
            "eSIM",
            "拍照",
            "扬声器",
            "麦克风",
            "振动",
            "传感器",
            "血压",
            "血氧",
            "心率",
            "心电图",
            "无线",
            "充电",
        ),
        phenomenon_values=(
            ("触控异常", ("触控失灵", "触控乱触")),
            ("卡或网络异常", ("SIM失灵", "无法安装", "eSIM异常", "网络无法连接")),
            ("拍照异常", ("无法打开拍照", "无法对焦", "成像异常")),
            ("声音异常", ("扬声器无声", "麦克风无声", "声音小", "杂音")),
            ("振动异常", ("无振动", "振动异响")),
            ("传感器或健康检测异常", ("传感器失灵", "血压失灵", "血氧失灵", "心率失灵", "心电图失灵")),
            ("无线或充电异常", ("NFC无响应", "WiFi无法连接", "蓝牙无法连接", "无法充电")),
        ),
        usage="手表触控、网络、拍照、声音、振动、传感器、健康检测、无线和充电是不同功能标准。",
    ),
    ClusteringJudgmentRule(
        rule_id="watch-repair-and-liquid",
        product_categories=("手表",),
        standard_family="手表拆修与浸液标准",
        merge_policy="separate_by_phenomenon",
        object_aliases=("屏幕", "电池", "螺丝", "外壳", "表圈", "手表"),
        phenomenon_values=(
            ("屏幕维修", ("内屏褶皱", "屏幕鼓包", "屏幕凸点", "屏幕印记", "更换外玻璃")),
            ("电池维修", ("电池更换", "更换电池")),
            ("螺丝拆修", ("螺丝拧动", "螺丝滑丝", "螺丝更换", "螺丝缺失")),
            ("外壳或表圈维修", ("外壳更换", "表圈更换", "开孔弧度异常")),
            ("撬痕溢胶或异物", ("溢胶", "撬痕", "异物", "缝隙不一致")),
            ("浸液或生锈", ("生锈", "发霉", "水渍")),
        ),
        usage="手表屏幕、电池、螺丝、外壳表圈和浸液是不同拆修路径，必须拆分。",
    ),
)


_BUILTIN_CLUSTERING_JUDGMENT_RULES = CLUSTERING_JUDGMENT_RULES
QUALITY_CLUSTERING_RULES_FILENAME = "quality_clustering_rules_10_categories.json"
QUALITY_CLUSTERING_RULES_PATH = Path(__file__).resolve().with_name(
    QUALITY_CLUSTERING_RULES_FILENAME
)
QUALITY_CLUSTERING_RULES_METADATA: dict[str, Any] = {
    "loaded": False,
    "source": "embedded_fallback",
    "schema_version": "",
    "curated_rule_count": len(_BUILTIN_CLUSTERING_JUDGMENT_RULES),
    "standard_family_index_count": 0,
    "terminology_entry_count": 0,
    "product_categories": [],
}
STANDARD_FAMILY_INDEX: tuple[StandardFamilyIndexEntry, ...] = ()


def _quality_rule_text(value: object) -> str:
    return str(value or "").strip()


def _quality_rule_texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        text
        for item in value
        if (text := _quality_rule_text(item))
    )


_LEGACY_SHARED_RULE_PRODUCT_TARGETS = {
    "相机机身": ("单电/微单机身", "单反机身"),
    "相机": ("单电/微单机身", "单反机身"),
    "camera_body": ("单电/微单机身", "单反机身"),
}


def _runtime_rule_product_categories(value: object) -> tuple[str, ...]:
    raw_values = (
        _quality_rule_texts(value)
        if isinstance(value, (list, tuple))
        else (_quality_rule_text(value),)
    )
    categories: list[str] = []
    for raw_value in raw_values:
        if not raw_value:
            continue
        shared_targets = _LEGACY_SHARED_RULE_PRODUCT_TARGETS.get(
            raw_value.casefold()
        )
        if shared_targets:
            categories.extend(shared_targets)
            continue
        category = canonical_product_name(raw_value, unknown="")
        if category:
            categories.append(category)
    return tuple(dict.fromkeys(categories))


def _quality_rule_phenomena(
    value: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[tuple[str, tuple[str, ...]]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        phenomenon_value = _quality_rule_text(item.get("phenomenon_value"))
        aliases = _quality_rule_texts(item.get("aliases"))
        if phenomenon_value and aliases:
            result.append((phenomenon_value, aliases))
    return tuple(result)


def _load_quality_clustering_rules_document() -> tuple[
    tuple[ClusteringJudgmentRule, ...],
    tuple[StandardFamilyIndexEntry, ...],
    dict[str, Any],
]:
    configured_path = _quality_rule_text(
        os.getenv("ANSWER_HUB_QUALITY_CLUSTERING_RULES_PATH")
    )
    path = Path(configured_path) if configured_path else QUALITY_CLUSTERING_RULES_PATH
    if not path.is_file():
        migrated_rules = tuple(
            replace(
                rule,
                product_categories=_runtime_rule_product_categories(
                    rule.product_categories
                ),
            )
            for rule in _BUILTIN_CLUSTERING_JUDGMENT_RULES
        )
        metadata = dict(QUALITY_CLUSTERING_RULES_METADATA)
        metadata["product_categories"] = sorted(
            {
                product
                for rule in migrated_rules
                for product in rule.product_categories
            }
        )
        return (
            migrated_rules,
            (),
            metadata,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"质检聚类口径 JSON 无法读取：{path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("质检聚类口径 JSON 顶层必须是对象")
    schema_version = _quality_rule_text(payload.get("schema_version"))
    curated_payload = payload.get("curated_clustering_rules")
    index_payload = payload.get("standard_family_index")
    if (
        not schema_version
        or not isinstance(curated_payload, list)
        or not isinstance(index_payload, list)
    ):
        raise RuntimeError(
            "质检聚类口径 JSON 缺少 schema_version、curated_clustering_rules "
            "或 standard_family_index"
        )

    curated_rules: list[ClusteringJudgmentRule] = []
    for item in curated_payload:
        if not isinstance(item, dict):
            continue
        rule_id = _quality_rule_text(item.get("rule_id"))
        standard_family = _quality_rule_text(item.get("standard_family"))
        if not rule_id or not standard_family:
            continue
        curated_rules.append(
            ClusteringJudgmentRule(
                rule_id=rule_id,
                product_categories=_runtime_rule_product_categories(
                    item.get("product_categories")
                ),
                standard_family=standard_family,
                merge_policy=_quality_rule_text(item.get("merge_policy")),
                object_aliases=_quality_rule_texts(item.get("object_aliases")),
                phenomenon_values=_quality_rule_phenomena(
                    item.get("phenomenon_values")
                ),
                usage=_quality_rule_text(item.get("usage")),
                category_l1=_quality_rule_text(item.get("category_l1")),
            )
        )

    standard_family_index: list[StandardFamilyIndexEntry] = []
    for item in index_payload:
        if not isinstance(item, dict):
            continue
        rule_id = _quality_rule_text(item.get("rule_id"))
        product_category = _quality_rule_text(item.get("product_category"))
        standard_family = _quality_rule_text(item.get("standard_family"))
        if not rule_id or not product_category or not standard_family:
            continue
        standard_family_index.append(
            StandardFamilyIndexEntry(
                rule_id=rule_id,
                product_category=product_category,
                category_l1=_quality_rule_text(item.get("category_l1")),
                category_l2=_quality_rule_text(item.get("category_l2")),
                standard_family=standard_family,
                merge_policy=_quality_rule_text(item.get("merge_policy")),
                subject_aliases=_quality_rule_texts(item.get("subject_aliases")),
                phenomenon_aliases=_quality_rule_texts(
                    item.get("phenomenon_aliases")
                ),
                merge_boundary=_quality_rule_text(item.get("merge_boundary")),
                decision_summary=_quality_rule_text(item.get("decision_summary")),
                detection_method_hint=_quality_rule_text(
                    item.get("detection_method_hint")
                ),
                exclusions_or_exceptions=_quality_rule_text(
                    item.get("exclusions_or_exceptions")
                ),
                source_reference=_quality_rule_text(item.get("source_reference")),
                source_type=_quality_rule_text(item.get("source_type")),
            )
        )

    if not curated_rules:
        raise RuntimeError("质检聚类口径 JSON 没有可用的精选聚类规则")
    products = sorted(
        {
            product
            for rule in curated_rules
            for product in rule.product_categories
            if product
        }
    )
    metadata = {
        "loaded": True,
        "source": "json",
        "path": str(path),
        "schema_version": schema_version,
        "curated_rule_count": len(curated_rules),
        "standard_family_index_count": len(standard_family_index),
        "terminology_entry_count": len(
            payload.get("terminology_dictionary")
            if isinstance(payload.get("terminology_dictionary"), list)
            else []
        ),
        "product_categories": products,
    }
    return tuple(curated_rules), tuple(standard_family_index), metadata


CLUSTERING_JUDGMENT_RULES, STANDARD_FAMILY_INDEX, QUALITY_CLUSTERING_RULES_METADATA = (
    _load_quality_clustering_rules_document()
)


def clustering_rules_metadata() -> dict[str, Any]:
    return dict(QUALITY_CLUSTERING_RULES_METADATA)


def _normalize(value: object) -> str:
    return str(value or "").strip().casefold()


def _normalize_products(
    product_categories: str | Iterable[str] | None,
) -> tuple[str, ...]:
    if product_categories is None:
        return ()
    values = (product_categories,) if isinstance(product_categories, str) else product_categories
    return tuple(
        dict.fromkeys(
            _normalize(canonical_product_name(value, unknown="") or value)
            for value in values
            if _normalize(value)
        )
    )


def _contains_any(text: str, aliases: tuple[str, ...]) -> bool:
    return any(_normalize(alias) in text for alias in aliases)


def _prompt_context_text(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(_prompt_context_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_prompt_context_text(item) for item in value)
    return _normalize(value)


def _match_standard_family_index_rule(
    *,
    product_category: str,
    subject: str,
    phenomenon: str,
    normalized_issue: str,
    conversation: str,
) -> ClusteringRuleMatch | None:
    product = _normalize(
        canonical_product_name(product_category, unknown="") or product_category
    )
    phenomenon_text = _normalize(phenomenon)
    core_text = " ".join(
        _normalize(value)
        for value in (subject, normalized_issue)
        if _normalize(value)
    )
    conversation_text = _normalize(conversation)
    candidates: list[tuple[int, StandardFamilyIndexEntry, str]] = []
    for entry in STANDARD_FAMILY_INDEX:
        if product not in {
            _normalize(item)
            for item in _runtime_rule_product_categories(entry.product_category)
        }:
            continue
        core_subject_match = _contains_any(core_text, entry.subject_aliases)
        conversation_subject_match = _contains_any(
            conversation_text,
            entry.subject_aliases,
        )
        if not (core_subject_match or conversation_subject_match):
            continue
        core_phenomenon_match = _contains_any(
            core_text,
            entry.phenomenon_aliases,
        )
        conversation_phenomenon_match = _contains_any(
            conversation_text,
            entry.phenomenon_aliases,
        )
        explicit_phenomenon_match = _contains_any(
            phenomenon_text,
            entry.phenomenon_aliases,
        )
        if entry.phenomenon_aliases and phenomenon_text and not (
            explicit_phenomenon_match
            or core_phenomenon_match
            or conversation_phenomenon_match
        ):
            continue
        score = (
            20 * int(core_subject_match)
            + 6 * int(conversation_subject_match)
            + 10 * int(explicit_phenomenon_match)
            + 4 * int(core_phenomenon_match)
            + 2 * int(conversation_phenomenon_match)
        )
        canonical_value = (
            entry.category_l2
            or next(
                (
                    alias
                    for alias in entry.phenomenon_aliases
                    if _normalize(alias)
                    in (phenomenon_text or core_text or conversation_text)
                ),
                "",
            )
        )
        candidates.append((score, entry, canonical_value))
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            -item[0],
            item[1].standard_family,
            item[1].category_l2,
        )
    )
    best_score = candidates[0][0]
    if best_score <= 0:
        return None
    top_candidates = [
        (entry, canonical_value)
        for score, entry, canonical_value in candidates
        if score == best_score
    ]
    top_boundaries = {
        (
            _normalize(entry.standard_family),
            _normalize(entry.merge_policy),
        )
        for entry, _canonical_value in top_candidates
    }
    if len(top_boundaries) > 1:
        # A tie across different standard families or merge policies is unsafe.
        return None
    best_entry, canonical_value = top_candidates[0]
    tied_values = {
        _normalize(value)
        for _entry, value in top_candidates
        if _normalize(value)
    }
    if len(tied_values) > 1:
        # Keep the shared family boundary without inventing one phenomenon value.
        canonical_value = ""
    usage = "；".join(
        value
        for value in (
            best_entry.decision_summary,
            best_entry.merge_boundary,
            best_entry.exclusions_or_exceptions,
        )
        if value
    )
    return ClusteringRuleMatch(
        rule_id=best_entry.rule_id,
        standard_family=best_entry.standard_family,
        merge_policy=best_entry.merge_policy,
        phenomenon_value=canonical_value,
        usage=usage,
        category_l1=best_entry.category_l1,
        category_l2=best_entry.category_l2,
        merge_boundary=best_entry.merge_boundary,
        exclusions_or_exceptions=best_entry.exclusions_or_exceptions,
        detection_method_hint=best_entry.detection_method_hint,
    )


def match_clustering_judgment_rule(
    *,
    product_category: str,
    subject: str = "",
    phenomenon: str = "",
    normalized_issue: str = "",
    conversation: str = "",
) -> ClusteringRuleMatch | None:
    product = _normalize(
        canonical_product_name(product_category, unknown="") or product_category
    )
    phenomenon_text = _normalize(phenomenon)
    core_text = " ".join(
        _normalize(value)
        for value in (subject, normalized_issue)
        if _normalize(value)
    )
    conversation_text = _normalize(conversation)
    matches: list[tuple[int, ClusteringRuleMatch]] = []
    for rule in CLUSTERING_JUDGMENT_RULES:
        if product not in {_normalize(item) for item in rule.product_categories}:
            continue
        core_object_matches = _contains_any(core_text, rule.object_aliases)
        conversation_object_matches = _contains_any(
            conversation_text,
            rule.object_aliases,
        )
        if not (core_object_matches or conversation_object_matches):
            continue
        phenomenon_matches = [
            (value, aliases)
            for value, aliases in rule.phenomenon_values
            if _contains_any(phenomenon_text, aliases)
        ]
        if phenomenon_text:
            # A structured phenomenon is the strongest signal. If it is not
            # one exact configured value, do not infer a hard rule from a
            # longer issue description that may contain several alternatives.
            if len(phenomenon_matches) != 1:
                continue
            value, aliases = phenomenon_matches[0]
            matched_alias_length = max(
                (
                    len(_normalize(alias))
                    for alias in aliases
                    if _normalize(alias) in phenomenon_text
                ),
                default=0,
            )
            score = (
                20
                + 3 * int(core_object_matches)
                + matched_alias_length
            )
        else:
            fallback_matches = [
                (value, aliases)
                for value, aliases in rule.phenomenon_values
                if (
                    _contains_any(core_text, aliases)
                    or _contains_any(conversation_text, aliases)
                )
            ]
            if len(fallback_matches) != 1:
                continue
            value, aliases = fallback_matches[0]
            score = (
                10 * int(_contains_any(core_text, aliases))
                + 3 * int(core_object_matches)
                + 2 * int(_contains_any(conversation_text, aliases))
                + int(conversation_object_matches)
            )
        matches.append(
            (
                score,
                ClusteringRuleMatch(
                    rule_id=rule.rule_id,
                    standard_family=rule.standard_family,
                    merge_policy=rule.merge_policy,
                    phenomenon_value=value,
                    usage=rule.usage,
                    category_l1=rule.category_l1,
                ),
            )
        )
    if matches:
        matches.sort(
            key=lambda item: (
                -item[0],
                item[1].standard_family,
                item[1].phenomenon_value,
            )
        )
        return matches[0][1]
    return _match_standard_family_index_rule(
        product_category=product_category,
        subject=subject,
        phenomenon=phenomenon,
        normalized_issue=normalized_issue,
        conversation=conversation,
    )


def build_clustering_rules_prompt_block(
    product_categories: str | Iterable[str] | None = None,
    *,
    context_values: Iterable[object] | None = None,
) -> str:
    requested = set(_normalize_products(product_categories))
    lines = [
        "=== 已固化的聚类判定口径（内置，非运行时标准关联）===",
        "硬边界：不同产品品类绝对不能聚类合并；相同查询目标、现象或标准族也不能突破品类边界。",
    ]
    for index, rule in enumerate(CLUSTERING_JUDGMENT_RULES, start=1):
        rule_products = {_normalize(item) for item in rule.product_categories}
        if requested and not requested.intersection(rule_products):
            continue
        values = "、".join(value for value, _aliases in rule.phenomenon_values)
        policy = (
            "同一标准族可作为同主题候选"
            if rule.merge_policy == "same_standard_family"
            else "不同现象值必须拆分"
        )
        lines.append(
            f"{index}. [{rule.standard_family}] 现象值：{values}。"
            f"聚类策略：{policy}。{rule.usage}"
        )
    context = _prompt_context_text(tuple(context_values or ()))
    if requested and context and STANDARD_FAMILY_INDEX:
        indexed_candidates: list[tuple[int, StandardFamilyIndexEntry]] = []
        for entry in STANDARD_FAMILY_INDEX:
            if not requested.intersection(
                {
                    _normalize(item)
                    for item in _runtime_rule_product_categories(
                        entry.product_category
                    )
                }
            ):
                continue
            subject_hits = sum(
                int(_normalize(alias) in context)
                for alias in entry.subject_aliases
                if _normalize(alias)
            )
            phenomenon_hits = sum(
                int(_normalize(alias) in context)
                for alias in entry.phenomenon_aliases
                if _normalize(alias)
            )
            category_hits = int(_normalize(entry.category_l1) in context) + int(
                _normalize(entry.category_l2) in context
            )
            score = 4 * subject_hits + 3 * phenomenon_hits + category_hits
            if score > 0:
                indexed_candidates.append((score, entry))
        indexed_candidates.sort(
            key=lambda item: (-item[0], item[1].standard_family, item[1].rule_id)
        )
        lines.append(
            "=== 当前品类相关标准族索引（仅用于识别候选，不代表自动合并）==="
        )
        seen_families: set[str] = set()
        for _score, entry in indexed_candidates:
            if entry.standard_family in seen_families:
                continue
            seen_families.add(entry.standard_family)
            subject_text = "、".join(entry.subject_aliases[:8]) or "待确认"
            phenomenon_text = "、".join(entry.phenomenon_aliases[:8]) or "查询目标"
            lines.append(
                f"- [{entry.standard_family}] "
                f"{entry.category_l1}/{entry.category_l2}；"
                f"对象：{subject_text}；现象/目标：{phenomenon_text}；"
                f"策略：{entry.merge_policy}；边界：{entry.merge_boundary}"
            )
            if len(seen_families) >= 16:
                break
    return "\n".join(lines)
