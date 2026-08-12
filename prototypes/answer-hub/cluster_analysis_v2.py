#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
答疑质检案例聚类分析脚本 V2
改进版：更精准的噪声过滤 + 更合理的主题合并/拆分逻辑
"""

import pandas as pd
import re
import json
from collections import defaultdict
from datetime import datetime

# ============================================================
# 1. 数据加载
# ============================================================
df = pd.read_excel('data/质检答疑案例库 (4).xlsx')
cols = ['uploader','upload_time','session_id','receiver','project','model','chat_log',
        'core_issue','judgment_result','judgment_basis','product_type','cat1','cat2',
        'reference_script','image_links','video_links']
df.columns = cols

# 修复project为nan的情况 - 使用product_type回填
for idx in df.index:
    if pd.isna(df.loc[idx, 'project']) or str(df.loc[idx, 'project']).strip() in ['nan', '']:
        pt = str(df.loc[idx, 'product_type'])
        if pt in ['手机', '电脑', '平板', '相机', '耳机', '镜头', '手表', '游戏机', '手写笔']:
            df.loc[idx, 'project'] = pt
        elif pt == '聚合回收':
            # 尝试从型号推断
            model = str(df.loc[idx, 'model'])
            if any(k in model for k in ['iPhone', '华为', '小米', 'OPPO', 'vivo', '荣耀', '三星', '红米']):
                df.loc[idx, 'project'] = '手机'
            elif any(k in model for k in ['MacBook', '联想', '华硕', '戴尔', '笔记本', 'RedmiBook']):
                df.loc[idx, 'project'] = '笔记本'
            else:
                df.loc[idx, 'project'] = pt

print(f"总案例数: {len(df)}")

# ============================================================
# 2. 增强版噪声过滤 (红线1,7,8)
# ============================================================

# 核心对象关键词 - 用来判断是否有具体判定对象
SPECIFIC_OBJECTS = [
    '屏幕', '内屏', '外屏', '显示屏', '触摸屏', 'OLED', 'LCD',
    '中框', '边框', '金属框', '手机边框', '外壳边框',
    '后壳', '后盖', '背板', '背壳', '底壳', 'D壳', 'D面', 'C壳', 'C面', 'A面', 'B面',
    '摄像头', '镜头', '相机', '前置摄像', '后置摄像', 'CMOS', '传感器',
    '电池', '电芯', '电池健康', '电池鼓包', '电池褶皱',
    '充电口', '尾插', 'Lightning', 'Type-C', 'USB口', '接口',
    '卡槽', 'SIM卡', '卡托', 'SIM',
    '按键', '电源键', '音量键', 'Home键', '拍照键', '静音键', '指纹键',
    '主板', '主板维修', 'CPU', '处理器', '芯片',
    '排线', '盖板', '连接线', 'IC',
    '防水标', '进水标', '浸液标',
    '序列号', 'IMEI', 'SN', '串号', '序列号标签',
    '硬盘', 'SSD', 'HDD', '固态硬盘', '机械硬盘', '内存', '运存', '显卡',
    '键盘', '键帽', 'keyboard', '触控板', 'Trackpad', 'touchpad',
    '转轴', '铰链', '合页', '脚垫',
    'WiFi', '蓝牙', '网络', '信号', '基带', '5G', '4G',
    '扬声器', '喇叭', '听筒', '麦克风', '风扇',
    '面容', '指纹', 'Face ID', 'Touch ID', '人脸识别',
    'BIOS', '系统锁', '激活锁', 'iCloud', '账号锁', 'ID锁',
    '充电器', '充电线', '电源适配器', '数据线', '配件',
    '全新机', '三码合一', '包装盒', '塑封',
    '胶条', '支架', '脚垫', '散热', '出风口',
    '取景器', '取景框', 'EVF', '滤镜',
    '耳罩', '头梁', '充电仓', '充电盒',
    '表带', '表盘', '表冠',
    '后盖', '摇杆', '手柄',
]

# 具体异常现象关键词
SPECIFIC_PHENOMENON = [
    '破损', '破碎', '断裂', '裂开', '碎裂', '裂缝', '裂纹',
    '磕碰', '磕点', '凹陷', '凹点', '碰伤', '撞伤', '伤',
    '划痕', '划伤', '刮痕', '刮伤', '磨损', '磨痕',
    '掉漆', '脱漆', '漆面',
    '变形', '翘起', '弯曲', '不平', '鼓包', '膨胀',
    '脱胶', '溢胶', '开胶', '胶水', '胶条脱落',
    '缝隙', '间隙', '松动', '晃动', '闭合不严',
    '漏液', '液晶', '漏光',
    '色斑', '色块', '颜色不均', '偏色', '泛黄', '泛红', '泛蓝', '发黄', '发红', '变色',
    '亮点', '亮斑', '坏点', '白光检测',
    '屏生线', '横纹', '竖线', '线条', '红线', '绿线', '黑线',
    '老化', '烧屏',
    '透图', '残影', '烧影',
    '闪屏', '花屏', '闪烁', '黑屏', '间歇性黑屏', '蓝屏',
    '偏光', '偏光异常', '偏振光',
    '进液', '浸液', '进水', '受潮', '发霉', '生锈', '锈蚀', '霉菌',
    '进灰', '灰尘', '异物', '脏污', '污渍', '污垢', '毛发', '印记', '油脂',
    '拆修', '维修', '修过', '换过', '更换', '第三方标识', '焊接', '非原装', '改装',
    '序列号异常', '序列号不符', '序列号缺失', '序列号不一致',
    '型号不符', '型号不一致', '改版机', '改装机',
    '信息不符', '配置不符', '容量差异', '爬虫数据不符',
    '功能异常', '失灵', '无法使用', '不能使用', '故障', '损坏', '不工作',
    '网络锁', '有锁', '监管锁', 'BIOS锁', '激活锁', 'ID锁', '账号锁',
    '异响', '杂音', '噪音', '声音异常',
    '不回收', '不可回收', '不予回收', '拒收',
    '发霉', '霉菌', '菌丝', '霉斑',
]

def is_noise_case_v2(row):
    """
    增强版噪声检测
    """
    core = str(row['core_issue']).strip() if not pd.isna(row['core_issue']) else ''
    chat = str(row['chat_log']).strip() if not pd.isna(row['chat_log']) else ''
    result = str(row['judgment_result']).strip() if not pd.isna(row['judgment_result']) else ''
    model = str(row['model']).strip() if not pd.isna(row['model']) else ''

    # 1. 空核心问题
    if not core or core == 'nan' or len(core) < 10:
        return True, "核心问题为空或过短(少于10字)"

    core_clean = core.replace(' ', '').replace('\n', '').replace('\r', '')

    # 2. 纯噪声
    noise_only = [
        '已加载全部', '预览', '预览图片', 'OK', '好的', '收到', '谢谢', '没事', '嗯', '哦',
        '知道了', '明白', '懂了', '好', '行', '对', '是的', '对的', '嗯嗯',
    ]
    if core_clean in noise_only:
        return True, "核心问题仅为噪声词"

    # 3. 检查是否有具体部位/对象
    has_object = False
    for obj in SPECIFIC_OBJECTS:
        if obj in core:
            has_object = True
            break

    # 4. 检查是否有具体异常现象/判定口径
    has_phenomenon = False
    for phen in SPECIFIC_PHENOMENON:
        if phen in core:
            has_phenomenon = True
            break

    # 5. 泛化确认检测（红线7）
    # "帮我看一下这个正常吧"、"图片状况是否符合标准" 等
    generic_confirm_patterns = [
        r'^(回收师|工程师)?.*(上传|发送|提供).*(图片|照片|视频).*(咨询|询问).*(是否正常|是否符合|是否合格|是否可以).*$',
        r'^.*(看下|看一下|帮忙看|确认下).*(这个|图片|照片).*(正常|符合|可以|行不行)[吧吗呢]?$',
        r'^.*图片状况是否符合.*$',
        r'^.*对.*(图片|照片).*(状况|状态|情况).*(是否|存在).*疑问.*$',
    ]

    if not has_object and not has_phenomenon:
        # 检查是否是"泛化确认无具体对象"
        for pat in generic_confirm_patterns:
            if re.search(pat, core):
                return True, f"泛化图片确认，无具体部位/异常/判定口径（红线7/8）"
        # 纯流程咨询但无具体对象
        if any(w in core for w in ['咨询', '疑问', '不确定', '希望']):
            if not has_object and not has_phenomenon:
                # 进一步检查 - 可能只是"帮我看看"这类
                if re.search(r'(帮|请|麻烦|让).*(看|确认|判断)', core) and not has_object:
                    return True, "泛化求助，无具体判定对象/标准（红线8）"

    # 6. 仅元数据无实质问题
    if core.startswith('回收师在') and not has_object and not has_phenomenon:
        # 可能是非常笼统的描述
        if len(core) < 30:
            return True, "描述过于笼统，缺乏具体判定信息"

    return False, ""


noise_cases = []
valid_indices = []

for idx in df.index:
    is_noise, reason = is_noise_case_v2(df.loc[idx])
    if is_noise:
        noise_cases.append((idx, reason))
    else:
        valid_indices.append(idx)

print(f"\n=== 噪声过滤结果 ===")
print(f"噪声案例: {len(noise_cases)} 条")
for idx, reason in noise_cases:
    core_preview = str(df.loc[idx, 'core_issue'])[:100]
    proj = str(df.loc[idx, 'project'])
    print(f"  [{idx}] ({proj}) {reason}")
    print(f"       核心问题: {core_preview}")
print(f"有效案例: {len(valid_indices)} 条")

# ============================================================
# 3. 主要判定对象提取（用于规则3的拆分判断）
# ============================================================

# 定义主要判定对象（粗粒度），用于决定是否拆分
MAJOR_OBJECTS = {
    # 手机/平板
    '屏幕(含胶条/支架)': ['屏幕', '内屏', '外屏', '显示屏', '胶条', '支架', '偏光', '偏光膜', '折痕'],
    '中框/边框/外壳': ['中框', '边框', '外壳', '后壳', '后盖', '机身', '背板'],
    '摄像头/镜头': ['摄像头', '镜头', 'CMOS', '前置摄像', '后置摄像'],
    '电池': ['电池', '电芯', '电池健康', '鼓包'],
    '充电口/尾插': ['充电口', '尾插', '充电接口', 'Lightning', 'Type-C'],
    '卡槽/SIM': ['卡槽', 'SIM', '卡托', '双卡', '读卡'],
    '按键': ['按键', '电源键', '音量键', 'Home键', '拍照键'],
    '主板': ['主板', 'CPU', '芯片'],
    '防水标/浸液': ['防水标', '浸液', '进水', '进液'],
    '序列号/IMEI': ['序列号', 'IMEI', 'SN', '串号'],
    '存储/内存/容量': ['存储', '内存', '容量', '硬盘', '运存', 'ROM'],
    '系统/账号': ['系统', '账号', 'ID', '激活锁', 'iCloud', '越狱', 'ROOT', '监管锁', 'BIOS锁'],
    '网络/信号': ['网络', 'WiFi', '蓝牙', '信号', '基带', '运营商'],
    '扬声器/声音': ['扬声器', '听筒', '声音', '麦克风', '喇叭'],
    '配件/包装': ['充电器', '配件', '包装', '塑封', '全新机', '三码合一'],
    '触控': ['触控', '触摸', '触屏'],
    '生物识别': ['面容', '指纹', 'Face ID', 'Touch ID'],
    '传感器': ['传感器', '距离感应', '光线感应', '振动', '震动'],

    # 笔记本专用
    '键盘/按键': ['键盘', '键帽', '按键', 'keyboard', '背光'],
    '触控板': ['触控板', '触摸板', 'Trackpad', 'trackpad'],
    '散热/风扇': ['散热', '风扇', '散热口', '散热片', '出风口', '硅脂'],
    '转轴/铰链': ['转轴', '铰链', '合页', '开合'],
    '笔记本外壳面': ['A面', 'B面', 'C面', 'D面', 'D壳', 'A壳', 'B壳', 'C壳'],
    '脚垫': ['脚垫', '胶垫'],
    '接口': ['接口', 'USB', 'HDMI', '网线口', '网口', 'Type-C口'],
    '硬盘(品牌/真伪)': ['硬盘', '固态', 'SSD', 'HDD', '品牌硬盘', '第三方硬盘'],
    '内存(品牌/容量)': ['内存', 'DDR', '内存条'],
    '显卡': ['显卡', 'GPU', '独显', '核显', '集成显卡'],

    # 相机/镜头
    '滤镜': ['滤镜', 'UV镜'],
    '取景器': ['取景器', 'EVF'],
    '转盘/拨轮': ['转盘', '拨轮', '旋钮'],
    '闪光灯': ['闪光灯', '热靴'],

    # 耳机
    '耳罩/头梁': ['耳罩', '头梁', '耳棉'],
    '充电仓': ['充电仓', '机仓', '充电盒'],

    # 手表
    '表带': ['表带', '表链'],
    '表盘': ['表盘', '表壳'],

    # 游戏机
    '摇杆/手柄': ['摇杆', 'Joy-Con', '手柄'],
    '游戏机后盖': ['后盖'],

    # 通用
    '型号/版本识别': ['型号', '小型号', '机型', '版本', '颜色', '国行', '港版', '美版'],
    '设备真伪/来源': ['真伪', '真假', '来源', '官网查询', '渠道'],
    '全新机标准': ['全新机', '未拆封', '未激活'],
    '充电器/配件合规': ['充电器', '充电线', '数据线', '电源适配器'],
}

def get_major_object(text):
    """从文本中识别主要判定对象（粗粒度）"""
    text = str(text)
    found = []
    for major_obj, keywords in MAJOR_OBJECTS.items():
        for kw in keywords:
            if kw in text:
                found.append(major_obj)
                break
    return found if found else ['综合/未明确']


# ============================================================
# 4. 聚类执行
# ============================================================

print(f"\n=== 执行聚类 ===")

# Step 1: 按品类(project)分组
project_groups = defaultdict(list)
for idx in valid_indices:
    proj = str(df.loc[idx, 'project'])
    project_groups[proj].append(idx)

print(f"品类数: {len(project_groups)}")

# Step 2: 在每个品类内，按 cat2 作为初始主题分组
#         然后在 cat2 内根据 主要判定对象 决定是否拆分

all_clusters = []  # 最终聚类结果

for project, proj_indices in sorted(project_groups.items()):
    # 按cat2分组
    cat2_groups = defaultdict(list)
    for idx in proj_indices:
        cat2 = str(df.loc[idx, 'cat2'])
        cat2_groups[cat2].append(idx)

    for cat2, cat2_indices in sorted(cat2_groups.items()):
        if len(cat2_indices) <= 1:
            # 单案例也是一个独立主题
            idx = cat2_indices[0]
            row = df.loc[idx]
            core = str(row['core_issue'])
            major_objs = get_major_object(core + ' ' + str(row['judgment_result']))

            all_clusters.append({
                'indices': [idx],
                'project': project,
                'cat1': str(row['cat1']),
                'cat2': cat2,
                'major_objects': major_objs,
                'size': 1,
            })
            continue

        # 对于多案例的cat2组，按主要判定对象拆分
        obj_groups = defaultdict(list)
        for idx in cat2_indices:
            row = df.loc[idx]
            core = str(row['core_issue']) + ' ' + str(row['judgment_result'])
            major_objs = get_major_object(core)
            # 使用主要对象的第一个作为分组键
            obj_key = major_objs[0] if major_objs else '综合'
            obj_groups[obj_key].append(idx)

        # 对每个对象组，检查是否还需要按标准路径拆分
        for obj_key, obj_indices in obj_groups.items():
            if len(obj_indices) <= 1:
                idx = obj_indices[0]
                row = df.loc[idx]
                core = str(row['core_issue'])
                major_objs = get_major_object(core + ' ' + str(row['judgment_result']))
                all_clusters.append({
                    'indices': [idx],
                    'project': project,
                    'cat1': str(row['cat1']),
                    'cat2': cat2,
                    'major_objects': major_objs,
                    'size': 1,
                })
                continue

            # 按判定标准路径再拆分
            # 定义标准大类
            def get_standard_path(row):
                """判断判定标准路径"""
                core = str(row['core_issue'])
                result = str(row['judgment_result'])
                combined = core + ' ' + result

                if any(w in combined for w in ['是否正常', '是否符合标准', '是否属于正常']):
                    if any(w in combined for w in ['外观', '掉漆', '磕碰', '划痕', '碎裂', '破损']):
                        return '外观合规判定'
                    if any(w in combined for w in ['显示', '屏幕', '漏液', '色斑']):
                        return '显示合规判定'
                    if any(w in combined for w in ['功能', '失灵', '故障']):
                        return '功能合规判定'
                    return '合规性确认'

                if any(w in combined for w in ['漏液', '色斑', '亮点', '坏点', '老化', '屏生线', '透图', '闪屏']):
                    return '显示缺陷判定'

                if any(w in combined for w in ['磕碰', '划痕', '掉漆', '碎裂', '破损', '磨损', '凹陷', '变形']):
                    return '外观缺陷判定(损伤类)'

                if any(w in combined for w in ['脱胶', '溢胶', '开胶', '缝隙']):
                    return '外观缺陷判定(结构类)'

                if any(w in combined for w in ['进灰', '异物', '脏污', '印记', '指纹']):
                    return '外观缺陷判定(洁净类)'

                if any(w in combined for w in ['拆修', '维修', '更换', '第三方', '焊接', '非原装']):
                    return '拆修判定'

                if any(w in combined for w in ['序列号', 'IMEI', '型号', '版本', '配置', '容量', '内存', '存储']):
                    return '设备信息核实'

                if any(w in combined for w in ['全新机', '三码合一', '未拆封', '包装']):
                    return '全新机标准判定'

                if any(w in combined for w in ['网络锁', 'BIOS锁', '激活锁', '账号锁', 'ID锁', '监管锁']):
                    return '锁定状态判定'

                if any(w in combined for w in ['浸液', '进水', '进液', '防水标', '发霉', '生锈']):
                    return '浸液判定'

                if any(w in combined for w in ['功能', '失灵', '故障', '无法使用', '不能', '坏']):
                    return '功能故障判定'

                if any(w in combined for w in ['不回收', '不可回收', '拒收']):
                    return '可回收性判定'

                return '其他标准判定'

            std_groups = defaultdict(list)
            for idx in obj_indices:
                row = df.loc[idx]
                std_path = get_standard_path(row)
                std_groups[std_path].append(idx)

            for std_path, std_indices in std_groups.items():
                major_objs_all = set()
                for idx in std_indices:
                    row = df.loc[idx]
                    core = str(row['core_issue']) + ' ' + str(row['judgment_result'])
                    major_objs_all.update(get_major_object(core))

                all_clusters.append({
                    'indices': std_indices,
                    'project': project,
                    'cat1': str(df.loc[std_indices[0], 'cat1']),
                    'cat2': cat2,
                    'major_objects': list(major_objs_all),
                    'standard_path': std_path,
                    'size': len(std_indices),
                })

# 按主题大小排序
all_clusters.sort(key=lambda c: c['size'], reverse=True)

# ============================================================
# 5. 生成主题标签
# ============================================================

def generate_label(c):
    """生成可读的主题标签"""
    project = c['project']
    cat1 = c['cat1']
    cat2 = c['cat2']
    objs = '、'.join(c.get('major_objects', ['未识别'])[:3])
    std = c.get('standard_path', '')
    size = c['size']

    # 生成简洁的主题名称
    if std:
        label = f"【{project}】{cat2} → {objs} | {std} | {size}条"
    else:
        label = f"【{project}】{cat2} → {objs} | {size}条"
    return label

# ============================================================
# 6. 输出结果
# ============================================================

print(f"\n=== 聚类结果 ===")
print(f"总案例: {len(df)}")
print(f"噪声排除: {len(noise_cases)}")
print(f"有效案例: {len(valid_indices)}")
print(f"聚类主题数: {len(all_clusters)}")

# 按品类统计主题
project_topic_count = defaultdict(int)
for c in all_clusters:
    project_topic_count[c['project']] += 1

print(f"\n=== 各品类主题分布 ===")
for proj, count in sorted(project_topic_count.items()):
    total_cases = sum(c['size'] for c in all_clusters if c['project'] == proj)
    print(f"  {proj}: {count}个主题, {total_cases}条案例")

# 输出所有主题
print(f"\n=== 主题详情（按大小排序）===")
for i, c in enumerate(all_clusters):
    label = generate_label(c)
    print(f"\n--- 主题{i+1}: {label} ---")

    # 显示该主题下的案例
    for j, idx in enumerate(c['indices'][:5]):
        row = df.loc[idx]
        core = str(row['core_issue'])[:120]
        result = str(row['judgment_result'])[:80]
        print(f"  [{idx}] {core}")
        print(f"       → {result}")
    if c['size'] > 5:
        print(f"  ... 共{c['size']}条案例")

# ============================================================
# 7. 保存Excel结果
# ============================================================
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# 详细结果
output_data = []
for i, c in enumerate(all_clusters):
    label = generate_label(c)
    for idx in c['indices']:
        row = df.loc[idx]
        output_data.append({
            '主题编号': i + 1,
            '主题标签': label,
            '品类': c['project'],
            '一级分类': c['cat1'],
            '二级分类': c['cat2'],
            '主要判定对象': '、'.join(c.get('major_objects', [])),
            '判定标准路径': c.get('standard_path', ''),
            '主题案例数': c['size'],
            '原始行号': idx,
            '会话ID': row['session_id'],
            '型号': row['model'],
            '核心问题': str(row['core_issue']),
            '判定结果': str(row['judgment_result']),
            '判定依据': str(row['judgment_basis'])[:1000],
            '聊天记录': str(row['chat_log'])[:500],
            '参考话术': str(row['reference_script'])[:500],
        })

output_df = pd.DataFrame(output_data)
output_path = f'data/聚类结果_{timestamp}.xlsx'
output_df.to_excel(output_path, index=False, engine='openpyxl')
print(f"\n详细结果: {output_path}")

# 主题摘要
summary_data = []
for i, c in enumerate(all_clusters):
    label = generate_label(c)
    typical_issues = []
    for idx in c['indices'][:5]:
        core = str(df.loc[idx, 'core_issue'])[:200]
        model = str(df.loc[idx, 'model'])[:50]
        typical_issues.append(f"[{model}] {core}")

    summary_data.append({
        '主题编号': i + 1,
        '主题标签': label,
        '品类': c['project'],
        '一级分类': c['cat1'],
        '二级分类': c['cat2'],
        '主要判定对象': '、'.join(c.get('major_objects', [])),
        '判定标准路径': c.get('standard_path', ''),
        '案例数': c['size'],
        '典型问题示例': '\n---\n'.join(typical_issues),
        '案例行号': ', '.join(str(idx) for idx in c['indices']),
    })

summary_df = pd.DataFrame(summary_data)
summary_path = f'data/主题摘要_{timestamp}.xlsx'
summary_df.to_excel(summary_path, index=False, engine='openpyxl')
print(f"主题摘要: {summary_path}")

# 噪声排除案例
if noise_cases:
    noise_data = []
    for idx, reason in noise_cases:
        row = df.loc[idx]
        noise_data.append({
            '原始行号': idx,
            '排除原因': reason,
            '品类': str(row['project']),
            '型号': str(row['model']),
            '核心问题': str(row['core_issue'])[:300],
            '聊天记录': str(row['chat_log'])[:300],
            '判定结果': str(row['judgment_result'])[:200],
        })
    noise_df = pd.DataFrame(noise_data)
    noise_path = f'data/噪声排除案例_{timestamp}.xlsx'
    noise_df.to_excel(noise_path, index=False, engine='openpyxl')
    print(f"噪声案例: {noise_path}")

print(f"\n=== 聚类完成 ===")
print(f"输出文件:")
print(f"  1. {output_path} - 所有案例及主题分配")
print(f"  2. {summary_path} - 主题摘要")
if noise_cases:
    print(f"  3. {noise_path} - 噪声排除案例")
