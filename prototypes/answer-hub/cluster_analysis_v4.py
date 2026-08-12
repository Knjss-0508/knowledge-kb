#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
答疑质检案例聚类分析 V4
混合策略：
1. 继承V3的基础聚类（品类 + 对象域 + 标准类型）
2. 对大于阈值的大主题，用「具体部件+异常类型」进一步拆分
3. 拆分后单例自动合并回最近的子主题
4. 小主题（≤阈值）保持不变
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

# ============================================================
# 2. 噪声过滤 (红线1,7,8)
# ============================================================

def has_valid_question(core_text, chat_text, judgment_text):
    core = str(core_text).strip() if not pd.isna(core_text) else ''
    if not core or core == 'nan' or len(core) < 8:
        return False, True, "核心问题为空或过短(<8字)"

    noise_words = ['已加载全部', '预览', 'OK', '好的', '收到', '谢谢', '没事', '嗯', '知道了', '明白', '懂了']
    core_clean = core.replace(' ', '').replace('\n', '')
    if core_clean in noise_words:
        return False, True, "核心问题仅含噪声词"

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
        '摇杆', '手柄', '后盖', '滤镜', '取景器', '偏光', '偏振',
    ]
    anomalies = [
        '破损', '破碎', '断裂', '裂开', '碎裂', '裂缝', '裂纹',
        '磕碰', '磕点', '凹陷', '凹点', '碰伤', '撞伤',
        '划痕', '划伤', '刮痕', '磨损', '磨痕', '掉漆', '脱漆',
        '变形', '翘起', '弯曲', '鼓包', '膨胀',
        '脱胶', '溢胶', '开胶', '胶水',
        '缝隙', '松动', '晃动', '闭合不严',
        '漏液', '液晶', '色斑', '色块', '偏色',
        '泛黄', '泛红', '泛蓝', '发黄', '发红', '变色',
        '亮点', '坏点', '屏生线', '横纹', '竖线',
        '老化', '烧屏', '透图', '残影', '闪屏', '花屏', '闪烁', '黑屏',
        '进液', '浸液', '进水', '受潮', '发霉', '生锈',
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

    if not has_part and not has_anomaly:
        vague_help = re.search(r'(帮|麻烦|请).*(看|确认|判断|鉴定)', core)
        if vague_help:
            has_specific_action = bool(re.search(r'(真伪|真假|定位|查找|绑定|解绑|激活|还原|重置|ROOT|越狱)', core))
            if not has_specific_action:
                return False, True, "纯泛化求助，无具体部位和异常现象（红线7/8）"

    if not has_part and not has_anomaly:
        generic_pattern = r'^.*(图片|照片).*(是否正常|是否符合|是否合格|状况|状态).*(存在|疑问|咨询|不确定).*$'
        if re.match(generic_pattern, core):
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
print(f"噪声过滤: {len(noise_cases)}条排除, {len(valid_indices)}条有效")

# ============================================================
# 3. 判定对象域 + 判定标准类型 (继承V3)
# ============================================================

OBJECT_DOMAIN = {
    '外观-屏幕': {
        'keywords': ['屏幕', '内屏', '外屏', '显示屏', '触摸屏', '屏', '胶条', '支架', '屏幕边缘', '屏幕支架', '折叠屏'],
        'cat2_match': ['屏幕及正面外观', '屏幕磕点'],
    },
    '外观-中框/外壳': {
        'keywords': ['中框', '边框', '后壳', '后盖', '外壳', '机身', '背板', '后盖与中框', '螺丝', '尾插螺丝', '卡槽', '卡托', '拍照按键', 'A壳', 'B壳', 'C壳', 'D壳', 'D面', 'C面', 'A面', 'B面', '脚垫', '触控板外观'],
        'cat2_match': ['中框及外壳外观', '磕碰掉漆', '触控板外观问题'],
    },
    '外观-摄像头': {
        'keywords': ['摄像头', '镜头', '摄像', '拍照键', '前摄', '后摄', '相机镜头', 'CMOS指纹', '镜头划痕', 'LiDAR', '激光雷达'],
        'cat2_match': [],
    },
    '外观-键盘/触控板': {
        'keywords': ['键盘', '键帽', '按键', '触控板', '触摸板', 'Trackpad', 'keyboard', '背光', '字母磨'],
        'cat2_match': ['按键功能'],
    },
    '显示-漏液/坏点/亮点': {
        'keywords': ['漏液', '液晶', '坏点', '亮点', '亮斑', '白光检测'],
        'cat2_match': ['漏液', '亮点亮斑'],
    },
    '显示-色斑/老化/偏色': {
        'keywords': ['色斑', '色块', '颜色不均', '偏色', '泛黄', '泛红', '泛蓝', '发黄', '发红', '老化', '烧屏', '变色', '偏蓝', '偏光异常'],
        'cat2_match': ['色斑', '老化', '其他显示问题'],
    },
    '显示-线/闪/透图': {
        'keywords': ['屏生线', '横纹', '竖线', '红线', '绿线', '黑线', '闪屏', '花屏', '闪烁', '透图', '残影', '黑屏', '间歇性黑屏', '蓝屏'],
        'cat2_match': ['屏生线', '闪屏/花屏', '透图', '横纹'],
    },
    '功能-摄像头': {
        'keywords': ['摄像头功能', '相机功能', '拍照', '成像', '取景', '相机倍数'],
        'cat2_match': ['摄像头功能'],
    },
    '功能-声音/扬声器': {
        'keywords': ['扬声器', '喇叭', '声音', '听筒', '麦克风', '杂音', '无声', '风扇声音', '转轴异响'],
        'cat2_match': ['声音功能'],
    },
    '功能-WiFi/蓝牙/网络': {
        'keywords': ['WiFi', 'wifi', '蓝牙', '网络', '信号', 'SIM卡', '通话', '基带', '无信号'],
        'cat2_match': ['无线功能', '通话功能', '网络制式'],
    },
    '功能-按键/触控/其他': {
        'keywords': ['按键功能', '电源键', '音量键', '触控', '触摸', '面容', '指纹', 'Face ID', '振动', '传感器', '定位', '键盘功能', '触控板功能'],
        'cat2_match': ['触控功能', '传感器功能', '振动功能', '生物识别功能', '按键功能'],
    },
    '拆修-主板': {
        'keywords': ['主板拆修', '主板维修', '主板', 'CPU', '焊接', '石墨纸', '屏蔽罩'],
        'cat2_match': ['主板拆修'],
    },
    '拆修-零部件': {
        'keywords': ['零部件拆修', '拆修痕迹', '第三方标识', '贴纸', '标签', '马克笔', '排线盖板', '非原厂', '维修痕迹'],
        'cat2_match': ['零部件拆修', '零部件拆修问题', '其他拆修痕迹', '零部件维修'],
    },
    '拆修-屏幕/后壳': {
        'keywords': ['屏幕拆修', '后壳拆修', '更换后壳', '更换屏幕', 'IMEI不一致', '后壳序列号'],
        'cat2_match': ['屏幕拆修', '屏幕拆修情况', '后壳拆修', '后壳拆修问题'],
    },
    '信息-型号/版本': {
        'keywords': ['型号', '小型号', '版本', '机型', '颜色', '国行', '港版', '美版', '日版', '改版机'],
        'cat2_match': ['小型号', '型号', '颜色', '购买渠道'],
    },
    '信息-序列号/来源': {
        'keywords': ['序列号', 'IMEI', 'SN', '串号', '来源', '官网', '查询', '真伪', '真假', '无法查询'],
        'cat2_match': ['设备来源'],
    },
    '信息-存储/配置': {
        'keywords': ['存储', '内存', '硬盘', '容量', '运存', '配置', 'SSD', 'HDD', '品牌硬盘', '品牌内存', '显卡'],
        'cat2_match': ['存储容量', '内存硬盘品牌', '硬盘品牌', 'cpu型号问题', '显卡功能'],
    },
    '信息-全新机标准': {
        'keywords': ['全新机', '三码合一', '未拆封', '未激活', '包装盒', '塑封', '防拆标签'],
        'cat2_match': ['全新机'],
    },
    '信息-账号/系统': {
        'keywords': ['账号', '激活锁', 'ID锁', 'iCloud', 'BIOS锁', '系统锁', '磁盘锁', '越狱', 'ROOT', '监管机', '演示机', '查找', '系统情况', '激活'],
        'cat2_match': ['账号状态', '系统情况'],
    },
    '电池': {
        'keywords': ['电池', '电芯', '电池健康', '健康度', '循环次数', '鼓包', '电池褶皱', '电池样式'],
        'cat2_match': ['电池健康度'],
    },
    '浸液': {
        'keywords': ['浸液', '进液', '进水', '发霉', '生锈', '锈蚀', '霉菌', '菌丝', '进水痕迹', '浸液痕迹'],
        'cat2_match': ['内部浸液', '外部浸液痕迹', '机身内部浸液'],
    },
    '配件/充电器': {
        'keywords': ['充电器', '充电线', '配件', '充电仓', '数据线', '电源适配器'],
        'cat2_match': ['充电器'],
    },
    '其他-特殊/综合': {
        'keywords': ['流程', '操作', '如何', '怎么', '特殊问题', '不回收', '开机情况',
                     '偏光膜', '偏振膜', '偏光', '偏振', '防水标', '进水标', '屏幕黑纸',
                     '排线褶皱', '开卡槽', '部件更换', '更换屏幕', '更换后壳', '更换外壳',
                     '摄像头缝隙', '镜片缝隙', '保护罩缝隙', '摄像头印记', '镜头印记'],
        'cat2_match': ['特殊问题', '不回收类型', '流程咨询', '其他问题', '其他功能问题', '其他显示异常', '开机情况',
                       '防水标', '屏幕拆修', '屏幕拆修情况', '后壳拆修', '后壳拆修问题'],
    },
}


def assign_object_domain(row):
    core = str(row['core_issue'])
    result = str(row['judgment_result'])
    basis = str(row['judgment_basis'])
    cat2 = str(row['cat2'])
    combined = core + ' ' + result + ' ' + basis

    # === 硬覆盖规则（来自分类提示词）===
    # 偏光问题 → 其他
    if any(w in combined for w in ['偏光', '偏振', '偏光膜', '偏振膜']):
        return '其他-特殊/综合'
    # 防水标 → 其他
    if '防水标' in combined:
        return '其他-特殊/综合'
    # 屏幕黑纸 → 其他
    if '黑纸' in combined:
        return '其他-特殊/综合'
    # 排线褶皱 → 其他
    if '排线褶皱' in combined or ('排线' in combined and '褶皱' in combined):
        return '其他-特殊/综合'
    # 开卡槽 → 其他（非表面磨损）
    if '开卡槽' in combined or ('卡槽' in combined and '开' in combined):
        return '其他-特殊/综合'
    # 摄像头镜片缝隙/保护罩缝隙 → 其他
    if any(w in combined for w in ['摄像头镜片有缝隙', '保护罩有缝隙', '镜片缝隙', '摄像头缝隙']):
        return '其他-特殊/综合'
    # 摄像头印记 → 其他
    if ('摄像头' in combined or '镜头' in combined) and '印记' in combined:
        return '其他-特殊/综合'
    # 部件更换（屏幕/后壳）→ 其他（排除"更换"作为维修判定时的情况）
    if any(w in combined for w in ['屏幕拆修', '后壳拆修', '更换后壳', '更换屏幕', '更换外壳', '换过屏幕', '换过壳']):
        if 'IMEI' not in combined:  # IMEI不一致时可能和后壳更换相关但关键是IMEI
            return '其他-特殊/综合'

    best_domain = None
    best_score = 0
    for domain, config in OBJECT_DOMAIN.items():
        score = 0
        for kw in config['keywords']:
            if kw in combined:
                score += 1
        if cat2 in config['cat2_match']:
            score += 3
        if score > best_score:
            best_score = score
            best_domain = domain
    if best_score == 0:
        return f'其他({cat2})'
    return best_domain


def get_standard_type(core, result):
    combined = (core + ' ' + result).lower()
    if any(w in combined for w in ['拆修', '维修痕迹', '第三方标识', '非原装', '更换后壳', '更换屏幕', '溢胶', '残胶', '硅脂']):
        if any(w in combined for w in ['痕迹', '标识', '贴纸', '标签', '马克笔', '溢胶', '残胶', '硅脂']):
            return '拆修-痕迹识别'
        return '拆修-更换判定'
    if any(w in combined for w in ['漏液', '坏点', '亮点', '亮斑', '白光检测']):
        return '显示-漏液/亮点判定'
    if any(w in combined for w in ['色斑', '色块', '偏色', '泛黄', '泛红', '老化', '变色']):
        return '显示-色斑/老化判定'
    if any(w in combined for w in ['屏生线', '横纹', '竖线', '闪屏', '花屏', '透图', '黑屏', '蓝屏']):
        return '显示-线条/闪烁判定'
    if any(w in combined for w in ['磕碰', '凹陷', '碎裂', '破损', '裂缝', '断裂', '掉漆', '划痕', '磨损']):
        return '外观-损伤判定'
    if any(w in combined for w in ['脱胶', '开胶', '缝隙', '松动', '翘起', '变形']):
        return '外观-结构/粘合判定'
    if any(w in combined for w in ['进灰', '异物', '脏污', '印记', '指纹', '灰尘', '生锈', '油污', '残留']):
        return '外观-洁净判定'
    if any(w in combined for w in ['正常', '符合标准', '是否属于正常', '原厂设计', '出厂']):
        return '外观-合规确认'
    if any(w in combined for w in ['功能异常', '失灵', '故障', '无法使用', '不能使用', '损坏', '不工作', '异常']):
        return '功能-故障判定'
    if any(w in combined for w in ['异响', '杂音', '噪音', '无声']):
        return '功能-声音异常判定'
    if any(w in combined for w in ['序列号', 'imei', 'sn码', '串号']):
        return '信息-序列号核实'
    if any(w in combined for w in ['型号', '版本', '小型号', '机型']):
        return '信息-型号核实'
    if any(w in combined for w in ['存储', '内存', '硬盘', '容量', '配置']):
        return '信息-配置核实'
    if any(w in combined for w in ['全新机', '三码合一', '未拆封', '未激活']):
        return '信息-全新机标准'
    if any(w in combined for w in ['账号', '激活锁', 'id锁', 'bios锁', '系统锁', '网络锁', '监管锁']):
        return '信息-锁定状态判定'
    if any(w in combined for w in ['真伪', '真假', '鉴别']):
        return '信息-真伪鉴别'
    if any(w in combined for w in ['浸液', '进水', '进液', '防水标', '发霉', '生锈']):
        return '浸液判定'
    if any(w in combined for w in ['电池', '鼓包', '健康度']):
        return '电池-状态判定'
    if any(w in combined for w in ['不回收', '不可回收', '拒收', '不能回收']):
        return '可回收性判定'
    return '其他判定'


# ============================================================
# 4. V4新增: 具体部件 + 异常类型 提取
# ============================================================

COMPONENT_KEYWORDS = {
    # 外观-屏幕
    '屏幕玻璃/面板': ['屏幕玻璃', '屏幕面板', '屏幕表层', '屏幕', '内屏', '外屏', '显示屏', '触摸屏', '屏面'],
    '胶条/支架': ['胶条', '支架', '屏幕支架', '屏幕胶条', '密封条', '缓冲胶', '屏幕边缘胶条'],
    '折叠屏铰链': ['折叠', '铰链', '折叠屏', '支架缺口', '折痕'],
    # 外观-中框/外壳
    '后壳/后盖': ['后壳', '后盖', '背板', '背壳', '皮质后壳', '皮壳', '玻璃后盖', '塑料后盖', '后盖与中框', '后壳颜色'],
    '边框/中框': ['中框', '边框', '边', '壳边缘', '框', '棱', '边框磕', '中框磕'],
    '卡托/卡槽': ['卡托', '卡槽', 'SIM卡托', 'SIM卡槽', '卡座', '卡针孔'],
    '螺丝/尾插': ['螺丝', '尾插螺丝', '底部螺丝', '尾插'],
    '按键/侧键': ['电源键', '音量键', '拍照键', '拍照按键', '静音键', 'Home键', '侧键'],
    '脚垫': ['脚垫', '防滑垫', '底垫', 'D面磨损'],
    '机身整体': ['机身', '机体', '整体'],
    # 外观-摄像头
    '摄像头镜片': ['镜片', '镜头玻璃', '镜头划痕', '前摄镜片', '后摄镜片', '镜头'],
    '摄像头边框': ['摄像头边框', '装饰圈', '摄像头圈', '镜圈', '摄像头环'],
    'CMOS/传感器': ['CMOS', '传感器', '图像传感器'],
    # 外观-键盘
    '键帽': ['键帽', '字母键', '按键', '键', '键帽缺失', '键帽磨损'],
    '触控板': ['触控板', '触摸板', 'Trackpad', '触控面板', '触控板外观'],
    # 拆修
    '第三方标识': ['第三方标识', '第三方标', '同行标', '友商标', '商家标', '第三方贴'],
    '排线/盖板': ['排线', '盖板', '排线盖板', 'FPC'],
    '马克笔痕迹': ['马克笔', '记号', '笔迹'],
    '后壳更换': ['后壳拆修', '更换后壳', '后壳序列号', 'IMEI不一致', '换壳'],
    '屏幕更换': ['屏幕拆修', '更换屏幕', '换屏', '屏幕更换', '屏幕IC', 'fog屏'],
    # 信息
    '小型号': ['小型号', '具体型号', '型号确认', '型号识别'],
    '序列号/IMEI': ['序列号', 'IMEI', 'SN码', '串号'],
    '存储/硬盘': ['存储', '内存', '硬盘', '容量', 'SSD', 'HDD', '配置'],
    '全新机': ['全新机', '三码合一', '未拆封', '未激活', '塑封', '防拆标签'],
    '账号锁': ['激活锁', 'ID锁', 'iCloud', 'BIOS锁', '系统锁', '监管锁', '网络锁', '账号'],
    # 功能
    '扬声器/喇叭': ['扬声器', '喇叭', '外放', '声音'],
    'WiFi/蓝牙': ['WiFi', 'wifi', '蓝牙', '无线'],
    'SIM/蜂窝': ['SIM卡', '蜂窝', '信号', '通话', '4G', '5G'],
    # 电池等
    '电池健康度': ['健康度', '循环次数', '电池寿命'],
    '防水标': ['防水标', '进水标', 'LDI'],
    '转轴/铰链': ['转轴', '铰链', 'hinge', '卷轴'],
}

ANOMALY_KEYWORDS = {
    '磕碰/凹陷': {'kw': ['磕碰', '磕点', '凹陷', '凹点', '碰伤', '撞伤', '磕', '碰', '坑'], 'exclude': ['掉漆']},
    '碎裂/破损': {'kw': ['碎裂', '破损', '裂缝', '断裂', '裂开', '破碎', '裂', '碎'], 'exclude': []},
    '划痕/磨损': {'kw': ['划痕', '划伤', '刮痕', '磨损', '磨痕', '刮擦', '磨', '蹭'], 'exclude': []},
    '掉漆/脱漆': {'kw': ['掉漆', '脱漆', '漆面', '掉色'], 'exclude': []},
    '变形/翘起': {'kw': ['变形', '翘起', '弯曲', '不平', '形变', '拱起'], 'exclude': []},
    '缺失/掉落': {'kw': ['缺失', '掉落', '缺少', '掉了', '缺损', '缺'], 'exclude': []},
    '缝隙/松动': {'kw': ['缝隙', '间隙', '松动', '晃动', '不严', '闭合不严'], 'exclude': []},
    '脱胶/开胶': {'kw': ['脱胶', '开胶', '胶开', '胶脱落', '胶分离', '粘合失效'], 'exclude': ['胶条', '溢胶']},
    '溢胶/残胶': {'kw': ['溢胶', '胶水', '胶痕', '残胶', '硅脂溢出', '胶溢出', '胶过多'], 'exclude': ['胶条', '脱胶', '开胶']},
    '脏污/印记': {'kw': ['脏污', '印记', '污渍', '污迹', '脏', '污', '垢', '印子'], 'exclude': []},
    '进灰/异物': {'kw': ['进灰', '灰尘', '异物', '灰', '尘', '粒'], 'exclude': []},
    '生锈/腐蚀': {'kw': ['生锈', '腐蚀', '锈蚀', '锈', '氧化'], 'exclude': []},
    '油污/残留': {'kw': ['油污', '油脂', '油', '残留物', '残留', '贴膜残留', '字符转移'], 'exclude': []},
    '漏液/液晶异常': {'kw': ['漏液', '液晶', '液', '液晶漏'], 'exclude': []},
    '坏点/亮点': {'kw': ['坏点', '亮点', '亮斑', '光点', '暗点'], 'exclude': []},
    '色斑/偏色': {'kw': ['色斑', '色块', '颜色不均', '偏色', '泛红', '泛蓝', '变色', '色差'], 'exclude': []},
    '老化/泛黄': {'kw': ['老化', '泛黄', '发黄', '烧屏', '偏黄', '黄斑'], 'exclude': []},
    '线条/条纹': {'kw': ['线', '横纹', '竖线', '条纹', '屏生线'], 'exclude': []},
    '闪烁/闪屏': {'kw': ['闪屏', '闪烁', '频闪', '花屏'], 'exclude': []},
    '透图/残影': {'kw': ['透图', '残影', '烙印', '鬼影'], 'exclude': []},
    '功能异常/失效': {'kw': ['失灵', '不工作', '无法使用', '不能使用', '故障', '不能用', '失效', '坏', '不正常'], 'exclude': []},
    '异响/杂音': {'kw': ['异响', '杂音', '噪音', '响声', '吱吱', '嘎嘎', '咔咔', '响', '吵'], 'exclude': []},
    '无法识别/确认': {'kw': ['无法识别', '无法确认', '识别不到', '确认不了', '无法确定', '不确定', '找不到', '无法判断'], 'exclude': []},
    '不匹配/不一致': {'kw': ['不匹配', '不一致', '不符', '不对', '矛盾', '对不上', '不一样', '差异'], 'exclude': []},
    '无法开机/黑屏': {'kw': ['无法开机', '不开机', '黑屏', '蓝屏', '白屏', '无显示', '不能开机'], 'exclude': []},
    '标准/口径确认': {'kw': ['标准', '口径', '规定', '规范', '政策', '是否属于', '是否按', '应判为', '应选', '如何处理', '如何判定'], 'exclude': ['标准缺失']},
    '操作/流程咨询': {'kw': ['如何操作', '怎么操作', '操作流程', '步骤', '方法', '流程'], 'exclude': []},
    '真伪/来源存疑': {'kw': ['真伪', '真假', '鉴别', '非原装', '非官方', '来源存疑', '疑似', '不是原装'], 'exclude': []},
    '可回收性': {'kw': ['不回收', '不可回收', '能否回收', '是否可回收', '不能回收'], 'exclude': []},
}


def extract_component(core, result):
    combined = core + ' ' + result
    best = None
    best_score = 0
    for comp_name, keywords in COMPONENT_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in combined:
                score += 1
        if score > best_score:
            best_score = score
            best = comp_name
    return best if best_score > 0 else '综合/未识别'


def extract_anomaly(core, result):
    combined = core + ' ' + result
    best = None
    best_score = 0
    for anom_name, config in ANOMALY_KEYWORDS.items():
        score = 0
        for kw in config['kw']:
            if kw in combined:
                score += 1
        for ex in config['exclude']:
            if ex in combined:
                score -= 2
        if score > best_score:
            best_score = score
            best = anom_name
    return best if best_score > 0 else '其他异常'


# ============================================================
# 5. 混合聚类：V3基础上对大主题细拆
# ============================================================

SPLIT_THRESHOLD = 5  # >5条案例的主题才考虑拆分

print(f"\n=== Phase 1: V3基础聚类 ===")

# 为所有案例分配属性
case_attrs = {}
for idx in valid_indices:
    row = df.loc[idx]
    core = str(row['core_issue'])
    result = str(row['judgment_result'])
    case_attrs[idx] = {
        'project': str(row['project']),
        'object_domain': assign_object_domain(row),
        'standard_type': get_standard_type(core, result),
        'component': extract_component(core, result),
        'anomaly': extract_anomaly(core, result),
        'cat1': str(row['cat1']),
        'cat2': str(row['cat2']),
    }

# V3聚类: 品类 + 对象域 + 标准类型
v3_clusters = defaultdict(list)
for idx in valid_indices:
    attrs = case_attrs[idx]
    key = (attrs['project'], attrs['object_domain'], attrs['standard_type'])
    v3_clusters[key].append(idx)

print(f"V3基础聚类: {len(v3_clusters)} 个主题")

# V3单案例合并: 同项目+同对象域+同标准类型的单案例合并
single_clusters_v3 = {k: v for k, v in v3_clusters.items() if len(v) == 1}
multi_clusters_v3 = {k: v for k, v in v3_clusters.items() if len(v) > 1}

unmatched_singles = {}
for key, indices in single_clusters_v3.items():
    project, obj_domain, std_type = key
    best_match_key = None
    for mc_key in multi_clusters_v3:
        mc_proj, mc_domain, mc_std = mc_key
        if mc_proj == project and mc_domain == obj_domain:
            if mc_std == std_type:
                best_match_key = mc_key
                break
    if best_match_key:
        multi_clusters_v3[best_match_key].extend(indices)
    else:
        unmatched_singles[key] = indices

v3_clusters = {**multi_clusters_v3, **unmatched_singles}
print(f"V3合并后: {len(v3_clusters)} 个主题")

# ============================================================
# Phase 2: 对大主题用部件+异常细分
# ============================================================

print(f"\n=== Phase 2: 大主题细拆分（阈值>{SPLIT_THRESHOLD}条）===")

all_clusters = []
split_count = 0

for (project, obj_domain, std_type), indices in v3_clusters.items():
    size = len(indices)

    if size <= SPLIT_THRESHOLD:
        # 小主题保持不变
        all_clusters.append({
            'indices': indices,
            'project': project,
            'object_domain': obj_domain,
            'standard_type': std_type,
            'component': max([case_attrs[i]['component'] for i in indices],
                           key=lambda c: sum(1 for i in indices if case_attrs[i]['component'] == c)),
            'anomaly': max([case_attrs[i]['anomaly'] for i in indices],
                         key=lambda a: sum(1 for i in indices if case_attrs[i]['anomaly'] == a)),
            'cat1': str(df.loc[indices[0], 'cat1']),
            'cat2': str(df.loc[indices[0], 'cat2']),
            'size': size,
        })
    else:
        # 大主题: 按部件+异常拆分
        sub_clusters = defaultdict(list)
        for idx in indices:
            attrs = case_attrs[idx]
            sub_key = (attrs['component'], attrs['anomaly'])
            sub_clusters[sub_key].append(idx)

        # 合并单例到最大的同部件子主题
        sub_list = []
        for (comp, anom), sub_indices in sub_clusters.items():
            sub_list.append({
                'indices': sub_indices,
                'component': comp,
                'anomaly': anom,
                'size': len(sub_indices),
            })

        sub_list.sort(key=lambda s: s['size'], reverse=True)

        # 把单例子主题合并到同部件的最小子主题
        merged_subs = set()
        for i, sub in enumerate(sub_list):
            if sub['size'] == 1 and len(sub_list) > 1:
                # 找同部件的最近子主题
                for j, target in enumerate(sub_list):
                    if i != j and target['size'] > 1 and target['component'] == sub['component']:
                        target['indices'].extend(sub['indices'])
                        target['size'] = len(target['indices'])
                        merged_subs.add(i)
                        break

        # 过滤掉空的
        sub_list = [s for i, s in enumerate(sub_list) if i not in merged_subs]

        for sub in sub_list:
            all_clusters.append({
                'indices': sub['indices'],
                'project': project,
                'object_domain': obj_domain,
                'standard_type': std_type,
                'component': sub['component'],
                'anomaly': sub['anomaly'],
                'cat1': str(df.loc[sub['indices'][0], 'cat1']),
                'cat2': str(df.loc[sub['indices'][0], 'cat2']),
                'size': sub['size'],
            })

        split_count += 1
        sub_comps = set(s['component'] for s in sub_list)
        print(f"  拆分 [{project}] {obj_domain} | {std_type}: {size}条 → {len(sub_list)}个子主题 (部件: {sub_comps})")

# 排序
all_clusters.sort(key=lambda c: c['size'], reverse=True)

print(f"\n最终主题数: {len(all_clusters)} (拆分了大主题: {split_count}个)")

# ============================================================
# 6. Phase 3: 全局合并规则（同品类+同部件+同异常的极小主题）
# ============================================================

# 把 ≤1条的聚类尝试合并到 ≥2条的同品类+同部件的聚类
merged_count = 0
single_clusters = [c for c in all_clusters if c['size'] == 1]
multi_clusters = [c for c in all_clusters if c['size'] >= 2]

for sc in single_clusters:
    # 尝试找同品类+同部件+同异常的多案例聚类
    best = None
    for mc in multi_clusters:
        if mc['project'] != sc['project']:
            continue
        if mc['component'] == sc['component'] and mc['anomaly'] == sc['anomaly']:
            best = mc
            break
    if best:
        best['indices'].extend(sc['indices'])
        best['size'] = len(best['indices'])
        sc['indices'] = []  # 清空原聚类
        sc['size'] = 0
        merged_count += 1

# 过滤已被合并的单例
all_clusters = [c for c in all_clusters if c['size'] > 0]
multi_clusters = [c for c in all_clusters if c['size'] >= 2]

# 第二波合并: 剩余单例 同品类+同部件+相似异常
remaining_singles = [c for c in all_clusters if c['size'] == 1]
similar_groups = [
    {'磕碰/凹陷', '碎裂/破损', '变形/翘起'},
    {'划痕/磨损', '掉漆/脱漆'},
    {'脏污/印记', '油污/残留', '进灰/异物'},
    {'缝隙/松动', '脱胶/开胶'},
    {'溢胶/残胶', '油污/残留', '脏污/印记'},
    {'功能异常/失效', '异响/杂音'},
    {'不匹配/不一致', '无法识别/确认', '标准/口径确认'},
    {'漏液/液晶异常', '坏点/亮点'},
    {'色斑/偏色', '老化/泛黄'},
    {'线条/条纹', '闪烁/闪屏', '透图/残影', '无法开机/黑屏'},
]

for sc in remaining_singles:
    best = None
    for mc in multi_clusters:
        if mc['project'] != sc['project'] or mc['component'] != sc['component']:
            continue
        # 同异常或相似异常
        if mc['anomaly'] == sc['anomaly']:
            best = mc
            break
        for group in similar_groups:
            if sc['anomaly'] in group and mc['anomaly'] in group:
                best = mc
                break
        if best:
            break
    if best:
        best['indices'].extend(sc['indices'])
        best['size'] = len(best['indices'])
        sc['indices'] = []  # 清空
        sc['size'] = 0
        merged_count += 1

# 重新构建（去重）
all_clusters = [c for c in all_clusters if c['size'] > 0]
for c in all_clusters:
    c['indices'] = list(dict.fromkeys(c['indices']))  # 去重保持顺序
    c['size'] = len(c['indices'])
all_clusters.sort(key=lambda c: c['size'], reverse=True)

print(f"合并单例: {merged_count}个")
print(f"最终主题数: {len(all_clusters)}")

# ============================================================
# 7. 统计
# ============================================================

total_clustered = sum(c['size'] for c in all_clusters)
print(f"\n=== V4混合聚类结果 ===")
print(f"总案例: {len(df)}, 噪声排除: {len(noise_cases)}, 有效: {len(valid_indices)}")
print(f"最终主题数: {len(all_clusters)}, 已聚类: {total_clustered}")

print(f"\n=== 大小分布 ===")
size_dist = defaultdict(int)
for c in all_clusters:
    size_dist[c['size']] += 1
for size in sorted(size_dist.keys(), reverse=True):
    print(f"  {size}条/主题: {size_dist[size]}个")

# ============================================================
# 8. 输出结果
# ============================================================

def make_label(c):
    proj = c['project']
    comp = c['component']
    anom = c['anomaly']
    size = c['size']
    # 如果component是综合的，用对象域标识
    if comp == '综合/未识别':
        return f"【{proj}】{c['object_domain']} | {anom} | {size}条"
    return f"【{proj}】{comp} | {anom} | {size}条"


print(f"\n=== 最大的30个主题 ===")
for i, c in enumerate(all_clusters[:30]):
    label = make_label(c)
    print(f"\n--- T{i+1}: {label} ---")
    for idx in c['indices'][:4]:
        row = df.loc[idx]
        print(f"  [{idx}] {str(row['core_issue'])[:120]}")
    if c['size'] > 4:
        print(f"  ... 共{c['size']}条")

# 保存Excel
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
            '具体部件': c['component'],
            '异常类型': c['anomaly'],
            '判定标准类型': c['standard_type'],
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
output_path = f'data/聚类结果_v4_{timestamp}.xlsx'
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
        '具体部件': c['component'],
        '异常类型': c['anomaly'],
        '判定标准类型': c['standard_type'],
        '案例数': c['size'],
        '典型问题示例': '\n---\n'.join(examples),
        '案例行号': ', '.join(str(idx) for idx in c['indices']),
    })

summary_df = pd.DataFrame(summary_data)
summary_path = f'data/主题摘要_v4_{timestamp}.xlsx'
summary_df.to_excel(summary_path, index=False, engine='openpyxl')
print(f"主题摘要: {summary_path}")

# 噪声
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
    noise_path = f'data/噪声排除案例_v4_{timestamp}.xlsx'
    noise_df.to_excel(noise_path, index=False, engine='openpyxl')
    print(f"噪声案例: {noise_path}")

# Markdown报告
with open(f'data/聚类分析报告_v4_{timestamp}.md', 'w', encoding='utf-8') as f:
    f.write(f"# 答疑质检案例聚类分析报告 V4\n\n")
    f.write(f"## 📊 数据概况\n\n")
    f.write(f"| 项目 | 数量 |\n|------|------|\n")
    f.write(f"| 原始案例总数 | {len(df)} 条 |\n")
    f.write(f"| 排除噪声案例 | {len(noise_cases)} 条 |\n")
    f.write(f"| 有效案例 | {len(valid_indices)} 条 |\n")
    f.write(f"| 聚类主题数 | {len(all_clusters)} 个 |\n")
    f.write(f"| 最大主题 | {all_clusters[0]['size']} 条 |\n")
    f.write(f"| 拆分大主题数 | {split_count} 个 |\n")
    f.write(f"| 合并单例数 | {merged_count} 个 |\n")

    f.write(f"\n## 🏷️ 主题列表\n\n")
    f.write(f"| # | 主题标签 | 案例数 |\n|---|---|---|\n")
    for i, c in enumerate(all_clusters):
        label = make_label(c)
        f.write(f"| {i+1} | {label} | {c['size']} |\n")

    f.write(f"\n## 📁 各品类主题分布\n\n")
    f.write(f"| 品类 | 主题数 | 案例数 |\n|---|---|---|\n")
    proj_stats = defaultdict(lambda: {'topics': 0, 'cases': 0})
    for c in all_clusters:
        proj = c['project']
        proj_stats[proj]['topics'] += 1
        proj_stats[proj]['cases'] += c['size']
    for proj in sorted(proj_stats.keys(), key=lambda p: proj_stats[p]['cases'], reverse=True):
        f.write(f"| {proj} | {proj_stats[proj]['topics']} | {proj_stats[proj]['cases']} |\n")

print(f"报告: data/聚类分析报告_v4_{timestamp}.md")
print(f"\n=== 完成 ===")
