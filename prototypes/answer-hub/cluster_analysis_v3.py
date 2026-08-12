#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
答疑质检案例聚类分析 V3
优化策略：
1. 增强噪声过滤
2. 使用项目+cat2作为基础聚类单元
3. 在cat2内部按"粗粒度判定对象域"拆分（规则3）
4. 判定标准路径使用粗粒度分类（规则4）
5. 合并单案例相邻聚类
"""

import pandas as pd
import re
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

# 修复project为nan的情况
for idx in df.index:
    if pd.isna(df.loc[idx, 'project']) or str(df.loc[idx, 'project']).strip() in ['nan', '']:
        pt = str(df.loc[idx, 'product_type'])
        model = str(df.loc[idx, 'model'])
        if pt in ['手机', '电脑', '平板', '相机', '耳机', '镜头', '手表', '游戏机', '手写笔']:
            df.loc[idx, 'project'] = pt
        elif '聚合回收' in pt:
            if any(k in model for k in ['iPhone','华为','小米','OPPO','vivo','荣耀','三星','红米','realme','努比亚']):
                df.loc[idx, 'project'] = '手机'
            elif any(k in model for k in ['MacBook','联想','华硕','戴尔','笔记本','RedmiBook','神舟','机械革命','雷神','火影','惠普','宏碁','微软']):
                df.loc[idx, 'project'] = '笔记本'
            else:
                df.loc[idx, 'project'] = pt
        else:
            df.loc[idx, 'project'] = pt

print(f"总案例数: {len(df)}")
print(f"品类: {sorted(df['project'].dropna().unique())}")

# ============================================================
# 2. 增强噪声过滤 (红线1,7,8)
# ============================================================

def has_valid_question(core_text, chat_text, judgment_text):
    """
    判断是否有有效用户问题
    返回: (is_valid, is_noise, reason)
    """
    core = str(core_text).strip() if not pd.isna(core_text) else ''
    chat = str(chat_text).strip() if not pd.isna(chat_text) else ''

    # 1. 空/过短
    if not core or core == 'nan' or len(core) < 8:
        return False, True, "核心问题为空或过短(<8字)"

    # 2. 纯噪声关键词
    noise_words = ['已加载全部', '预览', 'OK', '好的', '收到', '谢谢', '没事', '嗯', '知道了', '明白', '懂了']
    core_clean = core.replace(' ', '').replace('\n', '')
    if core_clean in noise_words:
        return False, True, "核心问题仅含噪声词"

    # 3. 泛化图片确认（红线7）- 无具体部位且无具体异常
    # 具体部位关键词
    parts = [
        '屏幕', '内屏', '外屏', '显示', '中框', '边框', '后壳', '后盖', '外壳', '机身',
        '摄像头', '镜头', 'CMOS', '电池', '充电口', '尾插', '卡槽', 'SIM', '卡托',
        '按键', '电源键', '音量键', 'Home键', '拍照键', '静音键', '指纹',
        '主板', 'CPU', '排线', '盖板', '防水标', '进水标',
        '序列号', 'IMEI', 'SN', '串号', '硬盘', '内存', '存储', '显卡',
        '键盘', '键帽', '触控板', '转轴', '铰链', '脚垫', '散热', '风扇',
        'WiFi', '蓝牙', '网络', '信号', '扬声器', '喇叭', '麦克风', '听筒',
        '面容', 'Face ID', 'Touch ID', 'BIOS', '激活锁', 'iCloud',
        '充电器', '充电线', '配件', '包装', '塑封', '三码合一',
        '胶条', '支架', '缝隙', '螺丝', '接口', 'USB', '网口',
        'A面', 'B面', 'C面', 'D面', 'AB面', 'CD面',
        '耳罩', '头梁', '充电仓', '充电盒', '表带', '表盘',
        '摇杆', '手柄', '后盖', '滤镜', '取景器',
    ]
    # 具体异常关键词
    anomalies = [
        '破损', '破碎', '断裂', '裂开', '碎裂', '裂缝', '裂纹',
        '磕碰', '磕点', '凹陷', '凹点', '碰伤', '撞伤',
        '划痕', '划伤', '刮痕', '磨损', '磨痕', '掉漆', '脱漆',
        '变形', '翘起', '弯曲', '鼓包', '膨胀',
        '脱胶', '溢胶', '开胶', '胶水',
        '缝隙', '松动', '晃动', '闭合不严',
        '漏液', '液晶', '漏光', '色斑', '色块', '偏色',
        '泛黄', '泛红', '泛蓝', '发黄', '发红', '变色',
        '亮点', '亮斑', '坏点', '屏生线', '横纹', '竖线',
        '老化', '烧屏', '透图', '残影', '闪屏', '花屏', '闪烁', '黑屏',
        '偏光', '偏振', '进液', '浸液', '进水', '受潮', '发霉', '生锈',
        '进灰', '灰尘', '异物', '脏污', '污渍', '印记', '油脂',
        '拆修', '维修', '修过', '换过', '更换', '第三方', '焊接', '非原装', '改装',
        '异常', '故障', '失灵', '损坏', '缺失', '不工作', '无法使用', '不能',
        '不符', '不一致', '不匹配', '错误', '不对', '差异',
        '不回收', '不可回收', '不予回收', '拒收',
        '网络锁', '监管锁', 'BIOS锁', '激活锁', 'ID锁', '账号锁',
        '异响', '杂音', '噪音',
    ]

    has_part = any(p in core for p in parts)
    has_anomaly = any(a in core for a in anomalies)

    # 纯泛化确认 - 无部位无异常
    # 关键判断：核心问题是否只有"确认是否正常/合格"而没有说明是什么部件/现象
    if not has_part and not has_anomaly:
        # 检查是否是"帮我看下/确认下"这类 - 需要结合是否有具体操作/流程描述
        vague_help = re.search(r'(帮|麻烦|请).*(看|确认|判断|鉴定)', core)
        if vague_help:
            # 如果有具体操作描述（如"定位移除"、"真伪鉴别"），不算噪声
            has_specific_action = bool(re.search(r'(真伪|真假|定位|查找|绑定|解绑|激活|还原|重置|ROOT|越狱)', core))
            if not has_specific_action:
                return False, True, "纯泛化求助，无具体部位和异常现象（红线7/8）"

    # 4. 泛化图片确认 - 仅有"正常/符合/合格"但无具体描述
    # 必须是纯图片确认+无具体描述才排除
    if not has_part and not has_anomaly:
        generic_pattern = r'^.*(图片|照片).*(是否正常|是否符合|是否合格|状况|状态).*(存在|疑问|咨询|不确定).*$'
        if re.match(generic_pattern, core):
            # 有型号/设备名也算有具体对象
            model_mentioned = bool(re.search(r'(iPhone|华为|小米|OPPO|vivo|荣耀|三星|MacBook|iPad|AirPods|Apple|Switch|Steam)', core))
            if not model_mentioned:
                return False, True, "纯图片确认，无具体部位/异常描述（红线7）"

    return True, False, ""


noise_cases = []
for idx in df.index:
    row = df.loc[idx]
    is_valid, is_noise, reason = has_valid_question(row['core_issue'], row['chat_log'], row['judgment_result'])
    if is_noise:
        noise_cases.append((idx, reason))

valid_indices = [idx for idx in df.index if idx not in [n[0] for n in noise_cases]]

print(f"\n噪声过滤: {len(noise_cases)}条排除, {len(valid_indices)}条有效")

# ============================================================
# 3. 主题建模 - 为每个案例分配主题标识
# ============================================================

# 定义粗粒度判定对象域 (用于规则3: 对象不同要拆分)
OBJECT_DOMAIN = {
    # 外观-屏幕
    '外观-屏幕': {
        'keywords': ['屏幕', '内屏', '外屏', '显示屏', '触摸屏', '屏', '胶条', '支架', '偏光膜', '偏振膜', '屏幕边缘', '屏幕支架', '折叠屏'],
        'cat2_match': ['屏幕及正面外观', '屏幕磕点'],
    },
    # 外观-中框/外壳
    '外观-中框/外壳': {
        'keywords': ['中框', '边框', '后壳', '后盖', '外壳', '机身', '背板', '后盖与中框', '螺丝', '尾插螺丝', '卡槽', '卡托', '拍照按键', 'A壳', 'B壳', 'C壳', 'D壳', 'D面', 'C面', 'A面', 'B面', '脚垫', '触控板外观'],
        'cat2_match': ['中框及外壳外观', '磕碰掉漆', '触控板外观问题'],
    },
    # 外观-摄像头
    '外观-摄像头': {
        'keywords': ['摄像头', '镜头', '摄像', '拍照键', '前摄', '后摄', '相机镜头', 'CMOS指纹', '镜头划痕', 'LiDAR', '激光雷达'],
        'cat2_match': [],
    },
    # 外观-键盘/触控板
    '外观-键盘/触控板': {
        'keywords': ['键盘', '键帽', '按键', '触控板', '触摸板', 'Trackpad', 'keyboard', '背光', '字母磨'],
        'cat2_match': ['按键功能'],
    },
    # 显示-漏液/坏点/亮点
    '显示-漏液/坏点/亮点': {
        'keywords': ['漏液', '液晶', '坏点', '亮点', '亮斑', '白光检测'],
        'cat2_match': ['漏液', '亮点亮斑'],
    },
    # 显示-色斑/老化/偏色
    '显示-色斑/老化/偏色': {
        'keywords': ['色斑', '色块', '颜色不均', '偏色', '泛黄', '泛红', '泛蓝', '发黄', '发红', '老化', '烧屏', '变色', '偏蓝', '偏光异常'],
        'cat2_match': ['色斑', '老化', '其他显示问题'],
    },
    # 显示-线条/闪屏/透图
    '显示-线/闪/透图': {
        'keywords': ['屏生线', '横纹', '竖线', '红线', '绿线', '黑线', '闪屏', '花屏', '闪烁', '透图', '残影', '黑屏', '间歇性黑屏', '蓝屏'],
        'cat2_match': ['屏生线', '闪屏/花屏', '透图', '横纹'],
    },
    # 功能-摄像头
    '功能-摄像头': {
        'keywords': ['摄像头功能', '相机功能', '拍照', '成像', '取景', '相机倍数'],
        'cat2_match': ['摄像头功能'],
    },
    # 功能-声音/扬声器
    '功能-声音/扬声器': {
        'keywords': ['扬声器', '喇叭', '声音', '听筒', '麦克风', '杂音', '无声', '风扇声音', '转轴异响'],
        'cat2_match': ['声音功能'],
    },
    # 功能-WiFi/蓝牙/网络
    '功能-WiFi/蓝牙/网络': {
        'keywords': ['WiFi', 'wifi', '蓝牙', '网络', '信号', 'SIM卡', '通话', '基带', '无信号'],
        'cat2_match': ['无线功能', '通话功能', '网络制式'],
    },
    # 功能-按键/传感器/其他
    '功能-按键/触控/其他': {
        'keywords': ['按键功能', '电源键', '音量键', '触控', '触摸', '面容', '指纹', 'Face ID', '振动', '传感器', '定位', '键盘功能', '触控板功能'],
        'cat2_match': ['触控功能', '传感器功能', '振动功能', '生物识别功能', '按键功能'],
    },
    # 拆修-主板
    '拆修-主板': {
        'keywords': ['主板拆修', '主板维修', '主板', 'CPU', '焊接', '石墨纸', '屏蔽罩'],
        'cat2_match': ['主板拆修'],
    },
    # 拆修-零部件
    '拆修-零部件': {
        'keywords': ['零部件拆修', '拆修痕迹', '第三方标识', '贴纸', '标签', '马克笔', '排线盖板', '非原厂', '维修痕迹'],
        'cat2_match': ['零部件拆修', '零部件拆修问题', '其他拆修痕迹', '零部件维修'],
    },
    # 拆修-屏幕/后壳
    '拆修-屏幕/后壳': {
        'keywords': ['屏幕拆修', '后壳拆修', '更换后壳', '更换屏幕', 'IMEI不一致', '后壳序列号'],
        'cat2_match': ['屏幕拆修', '屏幕拆修情况', '后壳拆修', '后壳拆修问题'],
    },
    # 信息-型号/版本
    '信息-型号/版本': {
        'keywords': ['型号', '小型号', '版本', '机型', '颜色', '国行', '港版', '美版', '日版', '改版机'],
        'cat2_match': ['小型号', '型号', '颜色', '购买渠道'],
    },
    # 信息-序列号/来源
    '信息-序列号/来源': {
        'keywords': ['序列号', 'IMEI', 'SN', '串号', '来源', '官网', '查询', '真伪', '真假', '无法查询'],
        'cat2_match': ['设备来源'],
    },
    # 信息-存储/配置
    '信息-存储/配置': {
        'keywords': ['存储', '内存', '硬盘', '容量', '运存', '配置', 'SSD', 'HDD', '品牌硬盘', '品牌内存', '显卡'],
        'cat2_match': ['存储容量', '内存硬盘品牌', '硬盘品牌', 'cpu型号问题', '显卡功能'],
    },
    # 信息-全新机标准
    '信息-全新机标准': {
        'keywords': ['全新机', '三码合一', '未拆封', '未激活', '包装盒', '塑封', '防拆标签'],
        'cat2_match': ['全新机'],
    },
    # 信息-账号/系统
    '信息-账号/系统': {
        'keywords': ['账号', '激活锁', 'ID锁', 'iCloud', 'BIOS锁', '系统锁', '磁盘锁', '越狱', 'ROOT', '监管机', '演示机', '查找', '系统情况', '激活'],
        'cat2_match': ['账号状态', '系统情况'],
    },
    # 电池
    '电池': {
        'keywords': ['电池', '电芯', '电池健康', '健康度', '循环次数', '鼓包', '电池褶皱', '电池样式'],
        'cat2_match': ['电池健康度'],
    },
    # 浸液
    '浸液': {
        'keywords': ['浸液', '进液', '进水', '防水标', '发霉', '生锈', '锈蚀', '霉菌', '菌丝', '进水痕迹'],
        'cat2_match': ['防水标', '内部浸液', '外部浸液痕迹', '机身内部浸液'],
    },
    # 配件/充电器
    '配件/充电器': {
        'keywords': ['充电器', '充电线', '配件', '充电仓', '数据线', '电源适配器'],
        'cat2_match': ['充电器'],
    },
    # 其他
    '其他-特殊/综合': {
        'keywords': ['流程', '操作', '如何', '怎么', '特殊问题', '不回收', '开机情况'],
        'cat2_match': ['特殊问题', '不回收类型', '流程咨询', '其他问题', '其他功能问题', '其他显示异常', '开机情况'],
    },
}

def assign_object_domain(row):
    """为案例分配判定对象域"""
    core = str(row['core_issue'])
    result = str(row['judgment_result'])
    basis = str(row['judgment_basis'])
    cat2 = str(row['cat2'])
    combined = core + ' ' + result + ' ' + basis

    best_domain = None
    best_score = 0

    for domain, config in OBJECT_DOMAIN.items():
        score = 0
        # 关键词匹配
        for kw in config['keywords']:
            if kw in combined:
                score += 1
        # cat2匹配加分
        if cat2 in config['cat2_match']:
            score += 3
        if score > best_score:
            best_score = score
            best_domain = domain

    if best_score == 0:
        # 回退到cat2推断
        return f'其他({cat2})'

    return best_domain


# ============================================================
# 4. 执行聚类
# ============================================================

print(f"\n=== 执行聚类 ===")

# 为每个有效案例分配对象域
case_domains = {}
for idx in valid_indices:
    case_domains[idx] = assign_object_domain(df.loc[idx])

# Step 1: 按品类分组
project_groups = defaultdict(list)
for idx in valid_indices:
    proj = str(df.loc[idx, 'project'])
    project_groups[proj].append(idx)

# Step 2: 在品类内按对象域聚类
all_clusters = []

for project, proj_indices in sorted(project_groups.items()):
    # 按对象域分组
    domain_groups = defaultdict(list)
    for idx in proj_indices:
        domain = case_domains[idx]
        domain_groups[domain].append(idx)

    for domain, domain_indices in sorted(domain_groups.items()):
        if len(domain_indices) <= 1:
            idx = domain_indices[0]
            all_clusters.append({
                'indices': [idx],
                'project': project,
                'cat1': str(df.loc[idx, 'cat1']),
                'cat2': str(df.loc[idx, 'cat2']),
                'object_domain': domain,
                'size': 1,
            })
            continue

        # 在对象域内，按判定标准粗粒度再拆分
        # 外观缺陷 vs 功能故障 vs 信息核实 vs 合规确认
        def get_standard_type(row):
            core = str(row['core_issue'])
            result = str(row['judgment_result'])
            combined = core + ' ' + result

            # 拆修判定
            if any(w in combined for w in ['拆修', '维修痕迹', '第三方标识', '非原装', '更换后壳', '更换屏幕']):
                if any(w in combined for w in ['痕迹', '标识', '贴纸', '标签']):
                    return '拆修-痕迹识别'
                return '拆修-更换判定'

            # 显示判定
            if any(w in combined for w in ['漏液', '坏点', '亮点', '亮斑', '白光检测']):
                return '显示-漏液/亮点判定'
            if any(w in combined for w in ['色斑', '色块', '偏色', '泛黄', '泛红', '老化', '变色']):
                return '显示-色斑/老化判定'
            if any(w in combined for w in ['屏生线', '横纹', '竖线', '闪屏', '花屏', '透图', '黑屏', '蓝屏']):
                return '显示-线条/闪烁判定'

            # 外观判定
            if any(w in combined for w in ['磕碰', '凹陷', '碎裂', '破损', '裂缝', '断裂', '掉漆', '划痕', '磨损']):
                return '外观-损伤判定'
            if any(w in combined for w in ['脱胶', '溢胶', '开胶', '缝隙', '松动', '翘起', '变形']):
                return '外观-结构/粘合判定'
            if any(w in combined for w in ['进灰', '异物', '脏污', '印记', '指纹', '灰尘']):
                return '外观-洁净判定'
            if any(w in combined for w in ['正常', '符合标准', '是否属于正常', '原厂设计', '出厂']):
                return '外观-合规确认'

            # 功能判定
            if any(w in combined for w in ['功能异常', '失灵', '故障', '无法使用', '不能使用', '损坏', '不工作', '异常']):
                return '功能-故障判定'
            if any(w in combined for w in ['异响', '杂音', '噪音', '无声']):
                return '功能-声音异常判定'

            # 信息判定
            if any(w in combined for w in ['序列号', 'IMEI', 'SN码', '串号']):
                return '信息-序列号核实'
            if any(w in combined for w in ['型号', '版本', '小型号', '机型']):
                return '信息-型号核实'
            if any(w in combined for w in ['存储', '内存', '硬盘', '容量', '配置']):
                return '信息-配置核实'
            if any(w in combined for w in ['全新机', '三码合一', '未拆封', '未激活']):
                return '信息-全新机标准'
            if any(w in combined for w in ['账号', '激活锁', 'ID锁', 'BIOS锁', '系统锁', '网络锁', '监管锁']):
                return '信息-锁定状态判定'

            # 浸液判定
            if any(w in combined for w in ['浸液', '进水', '进液', '防水标', '发霉', '生锈']):
                return '浸液判定'

            # 电池判定
            if any(w in combined for w in ['电池', '鼓包', '健康度']):
                return '电池-状态判定'

            # 可回收性
            if any(w in combined for w in ['不回收', '不可回收', '拒收', '不能回收']):
                return '可回收性判定'

            return '其他判定'

        std_groups = defaultdict(list)
        for idx in domain_indices:
            std_type = get_standard_type(df.loc[idx])
            std_groups[std_type].append(idx)

        for std_type, std_indices in std_groups.items():
            all_clusters.append({
                'indices': std_indices,
                'project': project,
                'cat1': str(df.loc[std_indices[0], 'cat1']),
                'cat2': str(df.loc[std_indices[0], 'cat2']),
                'object_domain': domain,
                'standard_type': std_type,
                'size': len(std_indices),
            })

# 按大小排序
all_clusters.sort(key=lambda c: c['size'], reverse=True)

# ============================================================
# 5. 合并小聚类（可选：将单案例聚类合并到相近主题）
# ============================================================

# 对项目+对象域相同的单案例聚类，尝试合并
merged_clusters = []
single_case_clusters = [c for c in all_clusters if c['size'] == 1]
multi_case_clusters = [c for c in all_clusters if c['size'] > 1]

# 为单案例找最相近的多案例聚类
unmatched_singles = []
for sc in single_case_clusters:
    best_match = None
    best_similarity = 0
    sc_proj = sc['project']
    sc_domain = sc['object_domain']

    for mc in multi_case_clusters:
        if mc['project'] != sc_proj:
            continue
        # 相同对象域优先
        if mc['object_domain'] == sc_domain:
            similarity = 3
        elif mc['object_domain'].split('-')[0] == sc_domain.split('-')[0]:
            similarity = 1
        else:
            continue

        if similarity > best_similarity:
            best_similarity = similarity
            best_match = mc

    if best_match and best_similarity >= 3:
        # 合并
        best_match['indices'].extend(sc['indices'])
        best_match['size'] = len(best_match['indices'])
    else:
        unmatched_singles.append(sc)

# 重新生成所有聚类
all_clusters = multi_case_clusters + unmatched_singles
# 重新计算size
for c in all_clusters:
    c['size'] = len(c['indices'])
all_clusters.sort(key=lambda c: c['size'], reverse=True)

# ============================================================
# 6. 输出结果
# ============================================================

def make_label(c):
    proj = c['project']
    domain = c['object_domain']
    std = c.get('standard_type', '')
    size = c['size']
    if std:
        return f"【{proj}】{domain} | {std} | {size}条"
    return f"【{proj}】{domain} | {size}条"

# 统计
total_clustered = sum(c['size'] for c in all_clusters)
print(f"\n=== 聚类结果 ===")
print(f"总案例: {len(df)}")
print(f"噪声排除: {len(noise_cases)}")
print(f"有效案例: {len(valid_indices)}")
print(f"聚类主题数: {len(all_clusters)}")
print(f"已聚类案例: {total_clustered}")

# 分布
print(f"\n=== 聚类大小分布 ===")
size_dist = defaultdict(int)
for c in all_clusters:
    size_dist[c['size']] += 1
for size in sorted(size_dist.keys(), reverse=True):
    print(f"  {size}条/主题: {size_dist[size]}个主题")

# 主题列表
print(f"\n=== 所有主题（前30）===")
for i, c in enumerate(all_clusters[:30]):
    label = make_label(c)
    print(f"\n--- 主题{i+1}: {label} ---")
    for idx in c['indices'][:3]:
        row = df.loc[idx]
        print(f"  [{idx}] {str(row['core_issue'])[:100]}")
    if c['size'] > 3:
        print(f"  ... 共{c['size']}条")

# ============================================================
# 7. 保存Excel
# ============================================================
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

output_data = []
for i, c in enumerate(all_clusters):
    label = make_label(c)
    for idx in c['indices']:
        row = df.loc[idx]
        output_data.append({
            '主题编号': i + 1,
            '主题标签': label,
            '品类': c['project'],
            '一级分类': c['cat1'],
            '二级分类': c['cat2'],
            '判定对象域': c['object_domain'],
            '判定标准类型': c.get('standard_type', ''),
            '主题案例数': c['size'],
            '原始行号': idx,
            '会话ID': row['session_id'],
            '型号': str(row['model']),
            '核心问题': str(row['core_issue']),
            '判定结果': str(row['judgment_result']),
            '判定依据': str(row['judgment_basis'])[:800],
            '聊天记录': str(row['chat_log'])[:400],
            '参考话术': str(row['reference_script'])[:400],
        })

output_df = pd.DataFrame(output_data)
output_path = f'data/聚类结果_{timestamp}.xlsx'
output_df.to_excel(output_path, index=False, engine='openpyxl')
print(f"\n详细结果: {output_path}")

# 主题摘要
summary_data = []
for i, c in enumerate(all_clusters):
    label = make_label(c)
    examples = []
    for idx in c['indices'][:5]:
        row = df.loc[idx]
        examples.append(f"[{str(row['model'])[:40]}] {str(row['core_issue'])[:200]}")

    summary_data.append({
        '主题编号': i + 1,
        '主题标签': label,
        '品类': c['project'],
        '判定对象域': c['object_domain'],
        '判定标准类型': c.get('standard_type', ''),
        '案例数': c['size'],
        '典型问题示例': '\n---\n'.join(examples),
        '案例行号': ', '.join(str(idx) for idx in c['indices']),
    })

summary_df = pd.DataFrame(summary_data)
summary_path = f'data/主题摘要_{timestamp}.xlsx'
summary_df.to_excel(summary_path, index=False, engine='openpyxl')
print(f"主题摘要: {summary_path}")

# 噪声案例
if noise_cases:
    noise_data = []
    for idx, reason in noise_cases:
        row = df.loc[idx]
        noise_data.append({
            '原始行号': idx,
            '排除原因': reason,
            '品类': str(row['project']),
            '核心问题': str(row['core_issue'])[:300],
        })
    noise_df = pd.DataFrame(noise_data)
    noise_path = f'data/噪声排除案例_{timestamp}.xlsx'
    noise_df.to_excel(noise_path, index=False, engine='openpyxl')
    print(f"噪声案例: {noise_path}")

print(f"\n=== 完成 ===")

# 打印所有主题的简洁列表（给人类看）
print(f"\n{'='*80}")
print(f"聚类结果总览")
print(f"{'='*80}")
for i, c in enumerate(all_clusters):
    label = make_label(c)
    print(f"  T{i+1}: {c['size']:>3}条 | {label}")
