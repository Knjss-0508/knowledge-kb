from __future__ import annotations

from answer_hub.terminology import (
    SUPPORTED_TERMINOLOGY_CATEGORIES,
    TERMINOLOGY_ENTRIES,
    build_terminology_prompt_block,
    find_terminology_entries,
    ensure_terminology_loaded,
    lookup_terminology,
    terminology_metadata,
)
from answer_hub.product_taxonomy import configured_product_names


def test_terminology_prompt_block_keeps_only_home_recycle_terms() -> None:
    block = build_terminology_prompt_block()

    assert "偏光检测" in block
    assert "红线检测" in block
    assert "已加载" in block
    assert "手机、平板电脑和笔记本场景中的验机工具或工具读数" in block
    for term in ("Xray", "X-ray检测", "GSX"):
        assert term not in block


def test_terminology_prompt_block_contains_remaining_product_terms() -> None:
    block = build_terminology_prompt_block()

    expected_definitions = {
        "DLC版": "实体游戏卡自带的追加内容版本",
        "金手指": "游戏卡带与主机接触读取的金属触点",
        "箱说齐全": "包装盒和说明资料齐全",
        "笔尖/笔芯/笔帽": "三个不同的手写笔部件",
        "磁吸配对": "吸附到匹配平板电脑后建立连接",
        "压感": "书写力度变化对应线条粗细变化",
        "扩展内存": "不计入设备实际运行内存",
        "断触/乱触/漂移": "三种不同的触控异常",
        "还原与解绑": "恢复出厂设置不一定解除账号绑定",
        "快门数": "相机累计执行快门动作的次数",
        "A/Av、S/Tv、P、M、B门": "相机曝光模式",
        "AF/MF": "自动对焦与手动对焦模式",
        "EXIF": "照片文件中的拍摄元数据",
        "热靴": "相机顶部连接闪光灯或附件的接口",
        "成像有斑": "瑕疵出现在拍摄照片中",
        "卡口": "镜头与相机机身连接的接口规格",
        "小痰盂": "低价50mm定焦镜头的俗称",
        "蒙皮/胶皮": "机身或镜头外部的橡胶覆盖层",
        "镜片进灰/异物/发霉": "三类不同的镜头内部现象",
        "镀膜与消光漆": "两个不同位置和用途的涂层",
        "光圈显示F--/F00/00/0": "机身未电子识别到镜头",
    }
    for term, definition in expected_definitions.items():
        assert term in block
        assert definition in block


def test_terminology_entries_expose_machine_readable_categories() -> None:
    assert TERMINOLOGY_ENTRIES
    assert all(entry.category.strip() for entry in TERMINOLOGY_ENTRIES)
    assert all(entry.product_categories for entry in TERMINOLOGY_ENTRIES)
    assert {entry.category for entry in TERMINOLOGY_ENTRIES} <= SUPPORTED_TERMINOLOGY_CATEGORIES

    block = build_terminology_prompt_block()
    for category in ("工具", "检测方法", "屏幕部件", "拆修现象", "浸液现象"):
        assert f"[{category}]" in block


def test_notebook_terms_keep_detection_parts_and_damage_categories_separate() -> None:
    expected_categories = {
        "鲁大师": "工具",
        "偏光工具": "检测方法",
        "偏光膜": "屏幕部件",
        "A面": "机身部件",
        "漏液": "屏幕显示现象",
        "水渍光斑": "浸液现象",
        "防水标签变红": "浸液现象",
        "摄像头镜片": "摄像头部件",
        "飞线": "拆修现象",
        "螺丝滑丝": "拆修现象",
    }

    for term, category in expected_categories.items():
        entry = lookup_terminology(term)
        assert entry is not None, term
        assert entry.category == category

    assert lookup_terminology("不存在的术语") is None


def test_terminology_prompt_block_contains_notebook_disambiguation_terms() -> None:
    block = build_terminology_prompt_block()

    expected_definitions = {
        "A/B/C/D面": "笔记本外壳和屏幕侧的四个位置名称",
        "设备锁": "账号、固件、BIOS或管理权限形成的设备限制",
        "充电器INPUT/OUTPUT": "充电器输入与输出参数标识",
        "CTO": "按订单定制的出厂配置",
        "偏光膜": "屏幕内部或表面的光学膜层部件",
        "透图": "屏幕残留先前图像或文字轮廓",
        "色斑/亮斑/亮点/坏点": "不同形态的屏幕显示异常",
        "屏幕气泡/磕点/印记": "屏幕表面的三类外观现象",
        "屏幕拆修痕迹": "屏幕或屏幕总成被拆卸、维修或更换的证据",
        "摄像头镜片与物理开关": "笔记本摄像头相关的外部部件",
        "主板拆修痕迹": "主板或内部零件被焊接、飞线或维修的证据",
        "板载扩容": "直接改动主板焊接存储或内存容量",
        "电池鼓包": "电池内部产气造成外壳鼓起",
        "浸液痕迹": "液体进入机身后留下的水渍、锈蚀或霉变证据",
    }
    for term, definition in expected_definitions.items():
        assert term in block
        assert definition in block


def test_terminology_lookup_uses_product_scope_for_ambiguous_aliases() -> None:
    notebook_entry = lookup_terminology("闪屏", product_category="笔记本")
    learning_entry = lookup_terminology("闪屏", product_category="学习机")

    assert notebook_entry is not None
    assert notebook_entry.term == "闪屏/花屏/屏线"
    assert learning_entry is not None
    assert learning_entry.term == "间歇性黑屏"
    assert lookup_terminology("闪屏") is None


def test_terminology_can_match_natural_text_with_longest_term_first() -> None:
    matches = find_terminology_entries(
        "笔记本屏幕偏光膜破损，主板有飞线和水渍光斑。",
        product_category="笔记本",
    )

    assert [entry.term for entry in matches] == [
        "偏光膜",
        "主板拆修痕迹",
        "浸液痕迹",
    ]
    assert "偏光检测" not in {entry.term for entry in matches}


def test_terminology_matches_screen_display_and_appearance_phrases_separately() -> None:
    matches = find_terminology_entries(
        "屏幕有亮斑、屏幕碎裂、屏幕划痕和屏幕涂层脱落。",
        product_category="笔记本",
    )

    assert [entry.term for entry in matches] == [
        "色斑/亮斑/亮点/坏点",
        "屏幕碎裂/划痕",
        "屏幕气泡/磕点/印记",
    ]


def test_terminology_prompt_block_filters_unrelated_product_terms() -> None:
    notebook_block = build_terminology_prompt_block(product_categories=("笔记本",))
    camera_block = build_terminology_prompt_block(product_categories=("相机镜头",))
    mirrorless_block = build_terminology_prompt_block(
        product_categories=("单电/微单机身",)
    )
    dslr_block = build_terminology_prompt_block(product_categories=("单反机身",))
    tablet_alias_block = build_terminology_prompt_block(product_categories=("平板",))
    game_card_block = build_terminology_prompt_block(
        product_categories=("游戏卡带",)
    )

    assert "A/B/C/D面" in notebook_block
    assert "DLC版" not in notebook_block
    assert "快门数" not in notebook_block
    assert "AF/MF" in camera_block
    assert "A/B/C/D面" not in camera_block
    assert "快门数" in mirrorless_block
    assert "快门数" in dslr_block
    assert "一根线" in tablet_alias_block
    assert "DLC版" in game_card_block
    assert "金手指" in game_card_block
    assert "A/B/C/D面" not in game_card_block


def test_terminology_metadata_is_stable_and_machine_readable() -> None:
    metadata = terminology_metadata()

    assert metadata["loaded"] is True
    assert metadata["source"] == "embedded"
    assert metadata["version"].startswith("terminology-")
    assert metadata["entry_count"] == len(TERMINOLOGY_ENTRIES)
    assert metadata["categories"] == sorted(metadata["categories"])
    assert "工具" in metadata["categories"]
    assert "笔记本" in metadata["product_categories"]
    assert set(metadata["product_categories"]) <= set(configured_product_names())
    assert ensure_terminology_loaded() == metadata


def test_empty_terminology_dictionary_is_rejected(monkeypatch) -> None:
    import answer_hub.terminology as terminology

    monkeypatch.setattr(terminology, "TERMINOLOGY_ENTRIES", ())

    try:
        ensure_terminology_loaded()
    except RuntimeError as exc:
        assert "术语字典未加载" in str(exc)
    else:
        raise AssertionError("空术语字典不得静默运行")
