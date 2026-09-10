from __future__ import annotations

from pathlib import Path

import answer_hub.clustering_rules as clustering_rules_module
from answer_hub.clustering_rules import (
    QUALITY_CLUSTERING_RULES_METADATA,
    STANDARD_FAMILY_INDEX,
    StandardFamilyIndexEntry,
    build_clustering_boundary_key,
    build_clustering_fingerprint,
    build_clustering_rules_prompt_block,
    clustering_threshold_values,
    clustering_rules_metadata,
    match_clustering_judgment_rule,
)
from answer_hub.product_taxonomy import configured_product_names


def test_phone_housing_damage_values_share_one_cluster_standard_family() -> None:
    cracked = match_clustering_judgment_rule(
        product_category="手机",
        subject="外壳",
        phenomenon="碎裂",
    )
    paint_loss = match_clustering_judgment_rule(
        product_category="手机",
        subject="后壳",
        phenomenon="掉漆",
    )

    assert cracked is not None
    assert paint_loss is not None
    assert cracked.standard_family == paint_loss.standard_family == "手机外壳外观标准"
    assert cracked.merge_policy == "same_standard_family"
    assert paint_loss.merge_policy == "same_standard_family"
    assert cracked.phenomenon_value == "碎裂"
    assert paint_loss.phenomenon_value == "掉漆"


def test_structured_boundary_key_allows_same_family_phenomena_on_same_object() -> None:
    cracked = build_clustering_boundary_key(
        business_line="自营回收",
        product_category="手机",
        subject="外壳",
        phenomenon="碎裂",
        normalized_issue="手机外壳碎裂如何判定",
    )
    paint_loss = build_clustering_boundary_key(
        business_line="自营回收",
        product_category="手机",
        subject="外壳",
        phenomenon="掉漆",
        normalized_issue="手机外壳掉漆如何判定",
    )

    assert cracked.complete
    assert paint_loss.complete
    assert cracked.as_tuple() == paint_loss.as_tuple()
    assert cracked.phenomenon_value == "*"


def test_structured_boundary_key_keeps_object_and_scope_boundaries_separate() -> None:
    housing = build_clustering_boundary_key(
        business_line="自营回收",
        product_category="手机",
        subject="外壳",
        phenomenon="碎裂",
        normalized_issue="手机外壳碎裂如何判定",
        platform="iOS",
    )
    frame = build_clustering_boundary_key(
        business_line="自营回收",
        product_category="手机",
        subject="中框",
        phenomenon="碎裂",
        normalized_issue="手机中框碎裂如何判定",
        platform="iOS",
    )
    android_housing = build_clustering_boundary_key(
        business_line="自营回收",
        product_category="手机",
        subject="外壳",
        phenomenon="碎裂",
        normalized_issue="手机外壳碎裂如何判定",
        platform="Android",
    )

    assert housing.complete
    assert frame.complete
    assert android_housing.complete
    assert housing.as_tuple() != frame.as_tuple()
    assert housing.as_tuple() != android_housing.as_tuple()


def test_threshold_boundary_ignores_model_numbers_in_standard_path() -> None:
    assert clustering_threshold_values("直径不超过0.5mm") == "直径不超过0.5mm"
    assert clustering_threshold_values("直径大于1mm") != clustering_threshold_values(
        "直径小于等于1mm"
    )
    boundary = build_clustering_boundary_key(
        product_category="手机",
        subject="屏幕",
        phenomenon="色斑",
        normalized_issue="iPhone 11 Pro屏幕色斑如何判定",
        standard_path="iPhone 11 Pro屏幕判定",
    )
    assert boundary.threshold_values == ""


def test_phone_screen_display_values_remain_separate_cluster_topics() -> None:
    leakage = match_clustering_judgment_rule(
        product_category="手机",
        subject="屏幕",
        phenomenon="漏液",
    )
    dead_pixel = match_clustering_judgment_rule(
        product_category="手机",
        subject="显示屏",
        phenomenon="坏点",
    )

    assert leakage is not None
    assert dead_pixel is not None
    assert leakage.standard_family == dead_pixel.standard_family == "手机屏幕显示标准"
    assert leakage.merge_policy == "separate_by_phenomenon"
    assert dead_pixel.merge_policy == "separate_by_phenomenon"
    assert leakage.phenomenon_value != dead_pixel.phenomenon_value


def test_notebook_query_fingerprints_keep_model_fingerprint_and_third_party_storage_separate() -> None:
    model = build_clustering_fingerprint(
        product_category="笔记本",
        subject="设备型号",
        phenomenon="型号待确认",
        normalized_issue="笔记本型号确认查询",
        judgment_target="根据序列号确认设备型号是否正确",
        resolution_mode="通过序列号和官网核对型号",
    )
    fingerprint = build_clustering_fingerprint(
        product_category="笔记本",
        subject="指纹功能",
        phenomenon="是否支持待确认",
        normalized_issue="笔记本指纹功能支持查询",
        judgment_target="确认该机型是否具备指纹功能",
        resolution_mode="根据具体型号核对官方配置",
    )
    third_party_storage = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="是否为第三方硬盘的判定",
        normalized_issue="笔记本硬盘是否为第三方硬盘",
        judgment_target="依据证据确认是否为第三方更换部件",
        resolution_mode="根据系统信息和现场证据判定",
    )
    true_tone = build_clustering_fingerprint(
        product_category="笔记本",
        subject="屏幕",
        phenomenon="原彩显示功能支持情况",
        normalized_issue="笔记本原彩显示功能支持情况",
        judgment_target="确认该机型是否具备原彩显示",
        resolution_mode="根据具体型号核对原彩显示功能",
    )

    assert model.query_target == "model_query"
    assert fingerprint.query_target == "fingerprint_support"
    assert third_party_storage.query_target == "memory_storage_third_party"
    assert true_tone.query_target == "true_tone_support"


def test_ultraviolet_detection_requirement_and_result_have_different_query_targets() -> None:
    requirement = build_clustering_fingerprint(
        product_category="手机",
        subject="屏幕拆修检测方法",
        phenomenon="紫光灯照射检测屏幕更换外玻璃印记",
        normalized_issue="iPhone 12 紫光灯检测是否强制",
        judgment_target="确认该机型是否需要使用紫光灯检测",
        resolution_mode="iPhone 12 不强制要求此检测项",
    )
    result = build_clustering_fingerprint(
        product_category="手机",
        subject="屏幕",
        phenomenon="紫光灯方格质检",
        normalized_issue="紫光灯检测结果为无以上问题",
        judgment_target="确认屏幕检测结果是否正常",
        resolution_mode="检测结果正常，判定为无以上问题",
    )

    assert requirement.query_target == "ultraviolet_detection_requirement"
    assert result.query_target == "ultraviolet_detection_result"


def test_phone_screen_leakage_with_color_explanation_keeps_leakage_value() -> None:
    match = match_clustering_judgment_rule(
        product_category="手机",
        subject="屏幕",
        phenomenon="漏液（显示绿色、粉色色块）",
    )

    assert match is not None
    assert match.standard_family == "手机屏幕显示标准"
    assert match.phenomenon_value == "漏液"


def test_headphone_find_rules_only_match_explicit_complete_statuses() -> None:
    normal = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找功能正常",
    )
    bound_account = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找功能已绑定账户",
    )
    abnormal = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找网络功能缺失",
    )
    unsupported = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="其他版本查找功能不支持",
    )
    not_detected = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找功能不检测",
    )
    incomplete_normal = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找功能检查",
        normalized_issue="查找 App 内有查找功能",
    )
    incomplete_unsupported = match_clustering_judgment_rule(
        product_category="耳机",
        subject="查找功能",
        phenomenon="查找功能检查",
        normalized_issue="外版有定位功能",
    )

    assert [
        normal.phenomenon_value if normal else "",
        bound_account.phenomenon_value if bound_account else "",
        abnormal.phenomenon_value if abnormal else "",
        unsupported.phenomenon_value if unsupported else "",
        not_detected.phenomenon_value if not_detected else "",
    ] == [
        "查找功能正常",
        "查找功能已绑定账户",
        "查找功能异常",
        "其他版本查找功能不支持",
        "查找功能不检测",
    ]
    assert incomplete_normal is None
    assert incomplete_unsupported is None


def test_ambiguous_threshold_topic_does_not_get_forced_into_one_screen_value() -> None:
    match = match_clustering_judgment_rule(
        product_category="平板电脑",
        subject="屏幕",
        phenomenon="点状瑕疵",
        normalized_issue="大于1mm为漏液，小于等于1mm为坏点",
    )

    assert match is None


def test_clustering_rules_prompt_keeps_merge_and_split_boundaries() -> None:
    prompt = build_clustering_rules_prompt_block(("手机",))

    assert "手机外壳外观标准" in prompt
    assert "碎裂、磕碰、掉漆、磨损" in prompt
    assert "同一标准族可作为同主题候选" in prompt
    assert "手机屏幕显示标准" in prompt
    assert "漏液、色斑、坏点、条纹" in prompt
    assert "不同现象值必须拆分" in prompt


def test_rules_cover_the_quality_standard_boundaries_of_all_products() -> None:
    cases = (
        (
            "手机",
            "屏幕",
            "漏液",
            "手机屏幕显示标准",
            "separate_by_phenomenon",
        ),
        (
            "平板电脑",
            "后壳",
            "掉漆",
            "平板外壳外观标准",
            "same_standard_family",
        ),
        (
            "笔记本",
            "A面",
            "划痕",
            "笔记本A面外观标准",
            "same_standard_family",
        ),
        (
            "手写笔",
            "手写笔",
            "无法配对",
            "手写笔使用状态标准",
            "separate_by_phenomenon",
        ),
        (
            "学习机",
            "屏幕",
            "闪屏",
            "学习机屏幕显示标准",
            "separate_by_phenomenon",
        ),
        (
            "相机镜头",
            "对焦环",
            "对焦环卡顿",
            "镜头操作环标准",
            "separate_by_phenomenon",
        ),
        (
            "单电/微单机身",
            "屏幕",
            "亮斑",
            "相机机身屏幕显示标准",
            "separate_by_phenomenon",
        ),
        (
            "单反机身",
            "屏幕",
            "亮斑",
            "相机机身屏幕显示标准",
            "separate_by_phenomenon",
        ),
        (
            "游戏卡带",
            "包装盒",
            "包装盒破损",
            "游戏卡带包装标准",
            "separate_by_phenomenon",
        ),
        (
            "耳机/耳麦",
            "麦克风",
            "送话无声",
            "耳机功能部件标准",
            "separate_by_phenomenon",
        ),
        (
            "智能手表",
            "屏幕",
            "漏液",
            "手表屏幕显示标准",
            "separate_by_phenomenon",
        ),
    )

    for (
        product_category,
        subject,
        phenomenon,
        standard_family,
        merge_policy,
    ) in cases:
        match = match_clustering_judgment_rule(
            product_category=product_category,
            subject=subject,
            phenomenon=phenomenon,
        )

        assert match is not None, product_category
        assert match.standard_family == standard_family
        assert match.merge_policy == merge_policy


def test_ambiguous_legacy_camera_body_does_not_select_a_runtime_rule_category() -> None:
    match = match_clustering_judgment_rule(
        product_category="相机机身",
        subject="屏幕",
        phenomenon="亮斑",
    )

    assert match is None


def test_notebook_surfaces_and_camera_lens_rings_keep_independent_boundaries() -> None:
    notebook_a = match_clustering_judgment_rule(
        product_category="笔记本",
        subject="A面",
        phenomenon="划痕",
    )
    notebook_c = match_clustering_judgment_rule(
        product_category="笔记本",
        subject="C面",
        phenomenon="划痕",
    )
    zoom_ring = match_clustering_judgment_rule(
        product_category="相机镜头",
        subject="变焦环",
        phenomenon="变焦环卡顿",
    )
    focus_ring = match_clustering_judgment_rule(
        product_category="相机镜头",
        subject="对焦环",
        phenomenon="对焦环卡顿",
    )

    assert notebook_a is not None
    assert notebook_c is not None
    assert notebook_a.standard_family != notebook_c.standard_family
    assert zoom_ring is not None
    assert focus_ring is not None
    assert zoom_ring.standard_family == focus_ring.standard_family
    assert zoom_ring.phenomenon_value != focus_ring.phenomenon_value


def test_query_fingerprint_keeps_different_information_targets_separate() -> None:
    battery = build_clustering_fingerprint(
        product_category="平板电脑",
        category_l1="信息查询",
        intent="信息查询",
        subject="电池",
        phenomenon="验机工具无法读取电池健康度",
        normalized_issue="平板电脑电池健康度读取异常如何处理",
        judgment_target="确认电池健康状态",
    )
    version = build_clustering_fingerprint(
        product_category="平板电脑",
        category_l1="基本情况",
        intent="信息查询",
        subject="设备版本",
        phenomenon="查询国行或港澳台版本",
        normalized_issue="平板电脑设备版本如何查询",
        judgment_target="确认设备版本",
    )

    assert battery.query_target == "battery_health"
    assert version.query_target == "device_version"
    assert battery.query_target != version.query_target


def test_serial_fingerprint_separates_read_failure_from_value_mismatch() -> None:
    read_failure = build_clustering_fingerprint(
        product_category="手机",
        category_l1="信息查询",
        intent="信息查询",
        subject="序列号",
        phenomenon="验机工具查询失败，序列号乱码",
        normalized_issue="序列号查询失败怎么处理",
    )
    mismatch = build_clustering_fingerprint(
        product_category="手机",
        category_l1="拆修问题",
        intent="标准判定",
        subject="后壳",
        phenomenon="后壳序列号与系统序列号不一致",
        normalized_issue="如何判定后壳是否更换",
    )

    assert read_failure.query_target == "serial_read_failure"
    assert mismatch.query_target == "serial_mismatch"
    assert read_failure.query_target != mismatch.query_target


def test_detection_fingerprint_separates_method_from_result() -> None:
    method = build_clustering_fingerprint(
        product_category="手机",
        category_l1="拆修问题",
        intent="检测核验",
        subject="屏幕",
        phenomenon="白光检测方法和观察角度",
        normalized_issue="白光检测怎么操作",
        judgment_target="确认检测方法是否正确",
    )
    result = build_clustering_fingerprint(
        product_category="手机",
        category_l1="拆修问题",
        intent="检测核验",
        subject="屏幕",
        phenomenon="白光检测下颜色异常",
        normalized_issue="白光检测结果异常如何判定",
        judgment_target="确认屏幕是否存在拆修问题",
    )

    assert method.query_target == "detection_method"
    assert result.query_target == "detection_result"
    assert method.query_target != result.query_target


def test_query_fingerprint_normalizes_notebook_model_and_separate_hardware_brand_queries() -> None:
    model = build_clustering_fingerprint(
        product_category="笔记本",
        category_l1="信息查询",
        intent="信息查询",
        subject="设备型号",
        phenomenon="想确认这是哪一款",
        normalized_issue="笔记本具体型号怎么核对",
        judgment_target="确认设备具体机型",
    )
    memory_brand = build_clustering_fingerprint(
        product_category="笔记本",
        category_l1="信息查询",
        intent="信息查询",
        subject="内存",
        phenomenon="是否属于品牌认证配件",
        normalized_issue="内存是不是品牌件",
        judgment_target="确认内存品牌属性",
    )
    storage_brand = build_clustering_fingerprint(
        product_category="笔记本",
        category_l1="信息查询",
        intent="信息查询",
        subject="硬盘",
        phenomenon="是否属于品牌认证配件",
        normalized_issue="硬盘是不是品牌件",
        judgment_target="确认硬盘品牌属性",
    )

    assert model.query_target == "model_query"
    assert memory_brand.query_target == "memory_brand"
    assert storage_brand.query_target == "storage_brand"
    assert memory_brand.query_target != storage_brand.query_target


def test_phone_waterproof_indicator_discoloration_uses_one_query_target() -> None:
    targets = {
        build_clustering_fingerprint(
            product_category="手机",
            subject=subject,
            phenomenon=phenomenon,
            normalized_issue=issue,
            judgment_target="确认防水标变色的判定口径",
        ).query_target
        for subject, phenomenon, issue in (
            ("防水标签", "变红", "手机防水标签变红如何判定"),
            ("卡槽防水标", "防水标变红", "手机卡槽防水标变红如何判定"),
            ("防水标", "局部变色", "手机防水标局部变色如何判定"),
        )
    }

    assert targets == {"waterproof_indicator_discoloration"}


def test_query_fingerprint_recognizes_named_memory_and_storage_brands() -> None:
    cxmt_memory = build_clustering_fingerprint(
        product_category="笔记本",
        subject="内存",
        phenomenon="内存品牌为CXMT（长鑫存储）",
        normalized_issue="笔记本内存品牌为CXMT如何判定",
        judgment_target="确认内存品牌是否正规",
    )
    named_storage = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="硬盘品牌查询",
        normalized_issue="笔记本硬盘品牌如何查询",
        judgment_target="确认硬盘品牌属性",
    )

    assert cxmt_memory.query_target == "memory_brand"
    assert named_storage.query_target == "storage_brand"


def test_storage_brand_third_party_wording_uses_storage_brand_target() -> None:
    third_party_brand = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="品牌是否为第三方",
        normalized_issue="笔记本硬盘品牌是否为第三方",
        judgment_target="确认硬盘品牌属性",
    )
    brand_part = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="是否为品牌件",
        normalized_issue="笔记本硬盘是否为品牌件",
        judgment_target="确认硬盘品牌属性",
    )
    replacement = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="是否为第三方更换部件",
        normalized_issue="笔记本硬盘是否为第三方更换部件",
        judgment_target="确认硬盘是否为第三方更换部件",
    )

    assert third_party_brand.query_target == "storage_brand"
    assert brand_part.query_target == "storage_brand"
    assert replacement.query_target == "memory_storage_third_party"


def test_huawei_storage_brand_ignores_memory_injected_by_capacity_alias() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="笔记本",
        subject="硬盘",
        phenomenon="第三方硬盘判定",
        normalized_issue="笔记本硬盘第三方硬盘如何判定",
        judgment_target="是否判定为第三方硬盘",
        standard_path="需有明确证据证明其为非原厂品牌或后更换部件",
        conversation=(
            "华为擎云 S540 只咨询硬盘是否要判第三方；"
            "图片仅显示硬盘容量信息，未显示品牌型号"
        ),
    )

    assert fingerprint.query_target == "storage_brand"


def test_huawei_memory_only_brand_stays_general_with_combined_source_context() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="笔记本",
        subject="内存",
        phenomenon="是否为品牌内存",
        normalized_issue="笔记本内存是否为品牌内存",
        judgment_target="确认内存是否为品牌件",
        conversation=(
            "华为 MateBook 14 当前原子问题只询问内存是否为品牌；"
            "来源摘要同时提到了内存和硬盘配置标准"
        ),
    )

    assert fingerprint.query_target == "memory_brand"


def test_business_fingerprint_recognizes_reusable_quality_topics() -> None:
    new_device = build_clustering_fingerprint(
        product_category="手机",
        category_l1="成色与回收标准",
        intent="标准判定",
        subject="整机",
        phenomenon="未拆包装且未激活",
        normalized_issue="是否可以按全新机回收",
        judgment_target="确认是否符合全新机条件",
    )
    camera_surface = build_clustering_fingerprint(
        product_category="手机",
        category_l1="外观问题",
        intent="标准判定",
        subject="后置摄像头镜头",
        phenomenon="镜头表面有印记和异物",
        normalized_issue="镜头印记/擦洗/异物/脏污如何判定",
        judgment_target="确认镜头表面状态",
    )
    notebook_color_spot = build_clustering_fingerprint(
        product_category="笔记本",
        category_l1="显示问题",
        intent="标准判定",
        subject="屏幕",
        phenomenon="灰色背景可见色斑",
        normalized_issue="笔记本屏幕色斑如何判定",
        judgment_target="确认是否属于屏幕色斑",
    )

    assert new_device.query_target == "new_device_eligibility"
    assert camera_surface.query_target == "camera_lens_surface_condition"
    assert notebook_color_spot.detection_target == "screen_color_spot"


def test_phone_new_device_fingerprint_accepts_parenthesized_new_wording() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="手机",
        category_l1="成色与回收标准",
        intent="标准判定",
        subject="包装盒防拆标签",
        phenomenon="是否影响判定",
        normalized_issue="手机（全新）｜包装盒防拆标签｜是否影响判定",
        judgment_target="判定为无影响",
        resolution_mode="按包装盒防拆标签标准处理",
    )

    assert fingerprint.query_target == "new_device_eligibility"


def test_phone_new_device_fingerprint_uses_context_for_tamper_label_boundary() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="手机",
        category_l1="成色与回收标准",
        intent="信息查询",
        subject="包装盒防拆标签",
        phenomenon="是否影响",
        normalized_issue="手机｜包装盒防拆标签｜是否影响｜确认为正常",
        judgment_target="标签状态判定",
        resolution_mode="客服查看图片给出判定",
        conversation="回收师咨询全新机这个防拆标不影响吧，客服确认标签没事。",
    )

    assert fingerprint.query_target == "new_device_eligibility"


def test_phone_new_device_fingerprint_uses_context_for_unactivated_regulatory_check() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="手机",
        category_l1="信息查询",
        intent="流程操作",
        subject="整机",
        phenomenon="未激活状态、监管信息查询",
        normalized_issue="未激活手机是否符合回收条件及具体操作流程",
        judgment_target="是否可回收及操作路径",
        resolution_mode="开机检查监管信息",
        conversation=(
            "回收师询问未激活手机是否回收。客服答复不能按全新机算，"
            "需开机查看有无监管信息。"
        ),
    )

    assert fingerprint.query_target == "new_device_eligibility"


def test_phone_new_device_fingerprint_excludes_activation_date_anomalies() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="手机",
        category_l1="基本情况",
        intent="异常核验",
        subject="激活信息",
        phenomenon="激活日期显示1970年",
        normalized_issue="手机（全新）激活日期异常如何判定",
        judgment_target="确认激活信息是否异常",
    )

    assert fingerprint.query_target != "new_device_eligibility"


def test_phone_new_device_context_does_not_override_activation_date_anomaly() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="手机",
        category_l1="基本情况",
        intent="异常核验",
        subject="激活信息",
        phenomenon="激活日期显示1970年",
        normalized_issue="手机激活日期异常如何判定",
        judgment_target="确认激活信息是否异常",
        conversation="用户说明这是全新机，但系统激活日期显示1970年。",
    )

    assert fingerprint.query_target != "new_device_eligibility"


def test_tablet_screen_red_or_yellow_shift_uses_one_aging_target() -> None:
    broad_shift = build_clustering_fingerprint(
        product_category="平板",
        category_l1="显示问题",
        intent="标准判定",
        subject="屏幕",
        phenomenon="屏幕泛红/发黄",
        normalized_issue="白底泛红或泛黄如何判定",
        judgment_target="判定是否为屏幕显示老化",
    )
    edge_shift = build_clustering_fingerprint(
        product_category="平板",
        category_l1="显示问题",
        intent="标准判定",
        subject="屏幕",
        phenomenon="屏幕边缘发红",
        normalized_issue="屏幕边缘发红如何判定",
        judgment_target="判定是否为屏幕显示老化",
    )

    assert broad_shift.detection_target == "screen_color_aging"
    assert edge_shift.detection_target == "screen_color_aging"


def test_primary_atomic_fields_prevent_other_topic_evidence_from_stealing_target() -> None:
    fingerprint = build_clustering_fingerprint(
        product_category="笔记本",
        category_l1="信息查询",
        intent="信息查询",
        subject="设备型号",
        phenomenon="型号待确认",
        normalized_issue="笔记本具体型号查询",
        judgment_target="确认设备具体型号",
        conversation="另一个原子问题讨论内存和硬盘是否为品牌认证。",
    )

    assert fingerprint.query_target == "model_query"


def test_quality_rules_json_is_loaded_as_the_runtime_rule_source() -> None:
    metadata = clustering_rules_metadata()
    packaged_rules_path = (
        Path(clustering_rules_module.__file__).resolve().parent
        / "quality_clustering_rules_10_categories.json"
    )

    assert QUALITY_CLUSTERING_RULES_METADATA["loaded"] is True
    assert metadata["source"] == "json"
    assert Path(metadata["path"]).resolve() == packaged_rules_path
    assert packaged_rules_path.is_file()
    assert metadata["curated_rule_count"] == 51
    assert metadata["standard_family_index_count"] == 426
    assert len(STANDARD_FAMILY_INDEX) == 426
    assert len(metadata["product_categories"]) == 11
    assert set(metadata["product_categories"]) <= set(configured_product_names())


def test_standard_family_index_fills_query_rule_not_in_curated_rules() -> None:
    match = match_clustering_judgment_rule(
        product_category="手机",
        subject="IMEI",
        normalized_issue="手机IMEI如何读取",
        conversation="在哪里查看IMEI",
    )

    assert match is not None
    assert match.rule_id == "source-2"
    assert "imei" in match.standard_family.casefold()
    assert match.merge_policy == "separate_by_query_target"


def test_standard_family_index_accepts_tied_entries_in_same_family(
    monkeypatch,
) -> None:
    shared = {
        "product_category": "学习机",
        "category_l1": "信息查询",
        "standard_family": "学习机测试查询标准",
        "merge_policy": "separate_by_query_target",
        "subject_aliases": ("测试并列对象",),
        "phenomenon_aliases": (),
        "merge_boundary": "同一查询目标可比较",
        "decision_summary": "测试并列索引",
        "detection_method_hint": "",
        "exclusions_or_exceptions": "",
        "source_reference": "test",
        "source_type": "test",
    }
    monkeypatch.setattr(
        clustering_rules_module,
        "STANDARD_FAMILY_INDEX",
        (
            StandardFamilyIndexEntry(
                rule_id="tie-a",
                category_l2="查询入口A",
                **shared,
            ),
            StandardFamilyIndexEntry(
                rule_id="tie-b",
                category_l2="查询入口B",
                **shared,
            ),
        ),
    )

    match = match_clustering_judgment_rule(
        product_category="学习机",
        subject="测试并列对象",
        normalized_issue="测试并列对象怎么查询",
    )

    assert match is not None
    assert match.standard_family == "学习机测试查询标准"
    assert match.merge_policy == "separate_by_query_target"


def test_prompt_includes_only_relevant_product_index_and_hard_product_boundary() -> None:
    entry = next(
        item
        for item in STANDARD_FAMILY_INDEX
        if item.product_category == "手机" and item.category_l2.casefold() == "imei"
    )
    tablet_entry = next(
        item
        for item in STANDARD_FAMILY_INDEX
        if item.product_category == "平板"
    )
    prompt = build_clustering_rules_prompt_block(
        ("手机",),
        context_values=(
            {
                "产品类型": "手机",
                "对象/部位": "IMEI",
                "核心问题": "手机IMEI如何读取",
            },
        ),
    )

    assert entry.standard_family in prompt
    assert tablet_entry.standard_family not in prompt
    assert "不同产品品类绝对不能聚类合并" in prompt
