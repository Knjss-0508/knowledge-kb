#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
答疑质检案例聚类分析脚本
按照8条红线规则对379条案例进行主题聚类
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

print(f"总案例数: {len(df)}")

# ============================================================
# 2. 红线1: 噪声过滤 - 没有有效用户问题的案例
# ============================================================
NOISE_KEYWORDS = [
    '已加载全部', '预览', 'OK', '好的', '收到', '谢谢', '没事', '嗯', '哦',
    '看一下这个正常吧', '图片状况是否符合标准', '帮我看一下',
]

def is_noise_case(row):
    """
    判断是否为噪声案例（没有有效用户问题）
    红线1: 没有有效用户问题的，不进自动聚类
    红线7: 泛化图片确认，默认不算有效主题
    红线8: 如果没有具体部位、异常现象、判定口径，先排除
    """
    core = str(row['core_issue']).strip()
    chat = str(row['chat_log']).strip()
    result = str(row['judgment_result']).strip()

    # 空核心问题
    if not core or core == 'nan' or len(core) < 5:
        return True, "核心问题为空或过短"

    # 仅噪声关键词
    core_clean = core.replace(' ', '').replace('\n', '')
    if core_clean in ['已加载全部', '预览', 'OK', '好的', '收到', '谢谢', '没事']:
        return True, "核心问题仅为噪声关键词"

    # 泛化图片确认 - 没有具体部位和异常现象
    generic_patterns = [
        r'^(回收师|工程师)?.*(上传|发送|提供).*(图片|照片|视频).*(咨询|询问|希望).*(是否正常|是否符合|是否合格).*$',
        r'^(回收师|工程师)?.*(看下|看一下|帮忙看).*(这个|图片|照片).*(正常|符合|可以).*$',
        r'^.*图片状况是否符合.*标准.*$',
    ]

    # 检查是否有具体部位描述
    has_specific_part = bool(re.search(
        r'屏幕|中框|外壳|后壳|电池|摄像头|镜头|充电口|尾插|卡槽|按键|键盘|触控板|转轴|散热|主板|'
        r'排线|扬声器|听筒|麦克风|指纹|面容|边框|胶条|支架|脚垫|螺丝|序列号|IMEI|'
        r'硬盘|内存|显卡|CPU|WiFi|蓝牙|SIM|信号|偏光|漏液|色斑|亮点|坏点|划痕|磕碰|'
        r'碎裂|脱胶|溢胶|进灰|进液|鼓包|变形|磨损|掉漆|老化|发霉|生锈|印记|脏污|异物|'
        r'防水标|BIOS|激活锁|账号|系统|型号|版本|存储|容量|配置',
        core
    ))

    has_specific_phenomenon = bool(re.search(
        r'破损|凹陷|划痕|磕碰|碎裂|裂缝|裂纹|漏液|色斑|亮点|亮斑|坏点|进灰|进液|进水|'
        r'脱胶|溢胶|鼓包|变形|磨损|掉漆|老化|发霉|生锈|印记|脏污|异物|泛白|泛黄|泛红|'
        r'发黄|发红|发蓝|偏光|偏色|闪屏|花屏|横纹|透图|黑屏|蓝屏|不显示|无显示|'
        r'异常|故障|失灵|损坏|缺失|不符|不一致|无法|不能',
        core
    ))

    # 泛化确认："看一下这个正常吧" 但没有具体对象
    if not has_specific_part and not has_specific_phenomenon:
        # 检查是否是纯泛化确认
        if re.search(r'(看下|看一下|帮忙看|确认).*(正常|符合|可以|行不行)', core):
            return True, "泛化确认无具体部位/异常/判定口径（红线7/8）"

    # 检查chat_log是否只有噪声
    if chat and chat != 'nan':
        chat_lines = [l.strip() for l in chat.split('\n') if l.strip()]
        meaningful_lines = [l for l in chat_lines if not re.match(
            r'^(已加载全部|预览|OK|好的|收到|谢谢|没事|嗯|哦|1|2|3|4|5|6|7|8|9|0|'
            r'\[图片|\[视频|知道了|明白|懂了|好|行|对|是|可以|是的|对的|嗯嗯|ok|OK)$', l)]
        if len(meaningful_lines) == 0:
            return True, "聊天记录仅含噪声内容"

    return False, ""


noise_cases = []
valid_cases = []

for idx, row in df.iterrows():
    is_noise, reason = is_noise_case(row)
    if is_noise:
        noise_cases.append((idx, reason))
    else:
        valid_cases.append(idx)

print(f"\n=== 噪声过滤结果 ===")
print(f"噪声案例: {len(noise_cases)} 条")
for idx, reason in noise_cases[:20]:
    print(f"  [{idx}] {reason}: {str(df.loc[idx, 'core_issue'])[:100]}")
if len(noise_cases) > 20:
    print(f"  ... 共 {len(noise_cases)} 条")
print(f"有效案例: {len(valid_cases)} 条")

# ============================================================
# 3. 多主题检测与拆分 (规则3的前置步骤)
# ============================================================
MULTI_TOPIC_MARKERS = [
    r'(同时|另外|此外|还|以及|并且).*(咨询|询问|疑问|问题)',
    r'(第[一二三四五六七八九十\d]+[个条]|问题[一二三四五六七八九十\d]+)',
    r'(\d+[\.\、])',
    r'(两个|三个|多个|几个).*(问题|疑问|咨询|质检点)',
]

def detect_multi_topic(core_issue, judgment_result, chat_log):
    """
    检测是否包含多个主题
    返回: [(子主题描述, 判定对象, 判定标准), ...] 或 None（单主题）
    """
    core = str(core_issue)
    result = str(judgment_result)

    # 检查judgment_result中是否有编号列表（说明有多个判定结论）
    numbered_results = re.findall(r'(\d+)[\.\、\s]+(.+?)(?=\d+[\.\、]|$)', result)
    if len(numbered_results) >= 2:
        # 确认是多个不同对象的判定
        objects_found = set()
        for num, text in numbered_results:
            objs = extract_judgment_targets(text)
            objects_found.update(objs)
        if len(objects_found) >= 2:
            return True

    # 检查是否有明确的多主题标记
    for pattern in MULTI_TOPIC_MARKERS:
        if re.search(pattern, core):
            return True

    return False


# ============================================================
# 4. 特征提取: 判定对象、判定标准、描述角度
# ============================================================

# 判定对象词典 - 按品类分组
OBJECT_DICT = {
    # 手机/平板
    '屏幕': ['屏幕', '内屏', '外屏', '显示屏', '显示', '屏', '正面'],
    '屏幕胶条': ['胶条', '屏幕边缘胶条', '屏幕胶'],
    '屏幕支架': ['支架', '折叠屏支架', '屏幕支架'],
    '中框': ['中框', '边框', '金属框', '手机边框'],
    '后壳/后盖': ['后壳', '后盖', '背板', '背壳', '后壳序列号', '后盖序列号'],
    '外壳': ['外壳', '机身外壳', '机壳', '壳体'],
    '摄像头/镜头': ['摄像头', '镜头', '相机', '摄像', '拍照', '前摄', '后摄', 'CMOS', '前置摄像头', '后置摄像头'],
    '电池': ['电池', '电芯', '电池健康', '电池健康度', '电池鼓包', '电池褶皱'],
    '充电口/尾插': ['充电口', '尾插', '充电接口', 'Lightning', 'Type-C', 'USB口'],
    '卡槽/SIM': ['卡槽', 'SIM', '卡托', '卡1', '卡2', '双卡', '单卡', '读卡'],
    '按键': ['按键', '电源键', '音量键', 'Home键', '拍照键', '静音键'],
    '主板': ['主板', '主板维修', '主板拆修', 'CPU', '处理器'],
    '排线': ['排线', '盖板', '连接线'],
    '防水标': ['防水标', '进水标', '浸液标'],
    '序列号/IMEI': ['序列号', 'IMEI', 'SN', '型号标签', '串号'],
    '存储/内存': ['存储', '内存', '容量', '硬盘', '运存', 'ROM', 'RAM', 'G', 'TB', 'GB'],
    '系统/账号': ['系统', '账号', 'ID', '激活锁', 'BIOS锁', '越狱', 'ROOT', '监管', 'iCloud'],
    '网络/信号': ['网络', '信号', 'WiFi', '蓝牙', '基带', '制式', '5G', '4G', '运营商'],
    '扬声器/声音': ['扬声器', '听筒', '声音', '麦克风', '喇叭', '杂音', '异响'],
    '偏光膜': ['偏光', '偏光膜', '偏振'],
    '生物识别': ['面容', '指纹', 'Face ID', 'Touch ID', '人脸'],
    '触控': ['触控', '触摸', '触屏', '点击'],
    '充电器/配件': ['充电器', '充电线', '数据线', '配件', '包装', '塑封', '全新'],
    '振动': ['振动', '震动'],
    '传感器': ['传感器', '距离感应', '光线感应'],

    # 笔记本专用
    '键盘': ['键盘', '键帽', '按键', 'keyboard', 'Key', '背光', '键位'],
    '触控板': ['触控板', '触摸板', 'Trackpad', 'trackpad'],
    '散热结构': ['散热', '风扇', '散热口', '散热片', '散热器', '出风口', '硅脂'],
    '转轴': ['转轴', '铰链', '合页', '开合'],
    'A面': ['A面', 'A壳', '顶盖'],
    'B面': ['B面', '屏幕边框', 'B壳'],
    'C面': ['C面', 'C壳', '掌托'],
    'D面': ['D面', 'D壳', '底壳', '底盖'],
    '脚垫': ['脚垫', '胶垫', '防滑垫'],
    '接口': ['接口', 'USB', 'HDMI', '网线口', '网口', '音频口', 'Type-C口'],
    '硬盘': ['硬盘', '固态', 'SSD', 'HDD', '机械硬盘', '固态硬盘', '品牌硬盘', '第三方硬盘'],
    '内存': ['内存', '内存条', 'DDR', '运存', '内存品牌'],
    '显卡': ['显卡', 'GPU', '独显', '核显', '集成显卡'],
    '电源/充电': ['电源', '充电', '适配器', '电源适配器'],

    # 相机/镜头
    '滤镜': ['滤镜', 'UV镜', 'ND镜', 'CPL'],
    '取景器': ['取景器', 'viewfinder', 'EVF'],
    'CMOS/传感器': ['CMOS', '传感器', 'CCD', '图像传感器'],
    '转盘/拨轮': ['转盘', '拨轮', '旋钮', '轮盘'],
    '闪光灯': ['闪光灯', '热靴'],

    # 耳机
    '耳罩': ['耳罩', '头梁', '耳棉', '耳垫'],
    '充电仓': ['充电仓', '机仓', '充电盒', '耳机盒'],

    # 手表
    '表带': ['表带', '表链', '腕带'],
    '表盘': ['表盘', '表壳', '表面', '屏幕'],

    # 游戏机
    '摇杆': ['摇杆', 'Joy-Con', '手柄'],
    '后盖': ['后盖', '背盖'],

    # 通用
    '设备来源/真伪': ['真伪', '真假', '来源', '国行', '港版', '美版', '日版', '版本', '官网', '查询'],
    '型号/版本': ['型号', '小型号', '版本', '机型', '设备型号', '具体型号'],
    '颜色': ['颜色', '配色', '金色', '银色', '黑色', '白色'],
    '购买渠道': ['购买渠道', '渠道', '购买来源'],
    '全新机判定': ['全新机', '全新未拆封', '未激活', '三码合一'],
}

# 判定标准/现象词典
STANDARD_DICT = {
    '破损/碎裂': ['破损', '碎裂', '裂缝', '裂纹', '破碎', '断裂', '裂开', '缺损'],
    '磕碰/凹陷': ['磕碰', '磕点', '凹陷', '碰伤', '撞击', '撞伤'],
    '划痕/磨损': ['划痕', '划伤', '磨损', '刮痕', '刮伤', '磨痕', '磨花'],
    '掉漆': ['掉漆', '脱漆', '漆面', '漆脱落'],
    '变形/翘起': ['变形', '翘起', '弯曲', '扭曲', '不平'],
    '脱胶/溢胶': ['脱胶', '溢胶', '开胶', '胶水', '胶条脱落', '粘胶'],
    '缝隙/间隙': ['缝隙', '间隙', '闭合不严', '不严丝合缝', '松动', '结合不紧'],
    '进灰/异物': ['进灰', '灰尘', '异物', '脏污', '污渍', '污垢', '毛发', '颗粒', '印记'],
    '漏液': ['漏液', '液晶泄漏', '液晶漏', '液漏'],
    '色斑/显示异常': ['色斑', '色块', '颜色不均', '颜色异常', '显示异常', '偏色', '泛黄', '泛红', '泛蓝', '泛白', '发黄', '发红', '变色', '偏蓝', '偏红', '偏黄'],
    '亮点/亮斑': ['亮点', '亮斑', '坏点', '白光检测', '亮点亮斑'],
    '屏生线/横纹': ['屏生线', '横纹', '竖线', '线条', '红线', '绿线', '黑线'],
    '老化': ['老化', '烧屏', '屏幕老化', '泛黄', '老化泛黄'],
    '透图/残影': ['透图', '残影', '烧影', '烙印'],
    '闪屏/花屏': ['闪屏', '花屏', '闪烁', '黑屏', '间歇性黑屏'],
    '偏光异常': ['偏光', '偏光异常', '偏振光', '光束'],
    '进液/浸液': ['进液', '浸液', '进水', '受潮', '进水痕迹', '浸液痕迹'],
    '发霉/生锈': ['发霉', '生锈', '锈蚀', '锈迹', '霉菌', '菌丝', '霉斑'],
    '鼓包': ['鼓包', '电池鼓包', '电池膨胀', '电池鼓起'],
    '拆修痕迹': ['拆修', '维修', '修过', '修理', '拆过', '换过', '更换', '第三方标识', '焊接', '焊锡'],
    '序列号异常': ['序列号', 'IMEI', 'SN码', '串号', '序列号异常', '序列号不符', '序列号缺失'],
    '型号不匹配': ['型号不符', '型号不一致', '型号识别', '型号错误', '改版机', '改装'],
    '配置不符': ['配置不符', '内存不符', '存储不符', '容量差异', '信息不符', '爬虫', '检测差异'],
    '功能异常': ['功能异常', '失灵', '不灵', '无法使用', '无法正常', '不能使用', '故障', '坏', '损坏'],
    '网络锁/有锁': ['网络锁', '有锁', '美版有锁', '卡贴', '运营商锁', '监管锁', 'MDM'],
    '账号锁/激活锁': ['账号锁', '激活锁', 'ID锁', 'iCloud锁', '查找', '定位', '演示机'],
    'BIOS锁/系统锁': ['BIOS锁', '系统锁', '磁盘锁', '管理员锁', '固件锁', '密码锁'],
    '声音异常': ['声音', '异响', '杂音', '噪音', '扬声器', '喇叭', '风扇声音', '转轴异响'],
    '外观正常/原厂设计': ['正常', '原厂设计', '出厂设计', '正常现象', '正常状态', '对称设计'],
    '全新机标准': ['全新机', '三码合一', '包装完整', '配件齐全', '未拆封', '未激活'],
    '不回收': ['不回收', '不可回收', '不予回收', '无法回收', '拒收', '拒绝回收'],
    '电池健康度': ['电池健康', '健康度', '循环次数', '电池寿命'],
    '版本/渠道': ['版本', '国行', '港版', '美版', '日版', '渠道', '购买渠道'],
    '刻字/标识': ['刻字', '刻痕', '文字', '标识', '标签', '贴纸', '马克笔'],
}

# 品类关键词映射
PROJECT_KEYWORDS = {
    '手机': ['手机', 'Phone', 'iPhone', '华为', '小米', 'OPPO', 'vivo', '荣耀', '三星', '红米', 'realme', '努比亚', '一加'],
    '笔记本': ['笔记本', '电脑', 'MacBook', 'ThinkPad', '联想', '华硕', '戴尔', '惠普', '神舟', '宏碁', '机械革命', '雷神', '微软', 'RedmiBook', '荣耀MagicBook', '火影'],
    '平板电脑': ['平板', 'iPad', 'Pad', '平板电脑'],
    '耳机': ['耳机', 'AirPods', 'Buds', 'EarPods'],
    '相机镜头': ['镜头', 'Lens'],
    '单反机身': ['单反', 'DSLR', 'EOS', 'D80', 'D5'],
    '单电/微单机身': ['微单', '单电', 'A7', 'Z6', 'R50', 'X-T', 'A5000'],
    '智能手表': ['手表', 'Watch', 'WATCH'],
    '游戏机': ['Switch', 'Steam Deck', '游戏机', '掌机', '任天堂'],
    '手写笔': ['手写笔', 'Pencil', '触控笔'],
    '无人机': ['无人机', 'DJI', '大疆', 'Osmo'],
    '三脚架/云台': ['三脚架', '云台'],
}

def extract_judgment_targets(text):
    """从文本中提取判定对象"""
    text = str(text)
    found = set()
    for category, keywords in OBJECT_DICT.items():
        for kw in keywords:
            if kw in text:
                found.add(category)
                break
    return found

def extract_standards(text):
    """从文本中提取判定标准"""
    text = str(text)
    found = set()
    for category, keywords in STANDARD_DICT.items():
        for kw in keywords:
            if kw in text:
                found.add(category)
                break
    return found

def extract_key_features(row):
    """
    从案例中提取关键特征
    返回: {
        'project': 品类,
        'product_type': 产品类型,
        'objects': 判定对象集合,
        'standards': 判定标准集合,
        'cat1': 一级分类,
        'cat2': 二级分类,
        'is_multi_topic': 是否多主题,
        'core_summary': 核心问题摘要
    }
    """
    core = str(row['core_issue'])
    result = str(row['judgment_result'])
    basis = str(row['judgment_basis'])
    chat = str(row['chat_log'])
    cat1 = str(row['cat1'])
    cat2 = str(row['cat2'])

    combined = core + ' ' + result + ' ' + basis

    objects = extract_judgment_targets(combined)
    standards = extract_standards(combined)
    is_multi = detect_multi_topic(core, result, chat)

    return {
        'objects': objects,
        'standards': standards,
        'is_multi_topic': is_multi,
        'cat1': cat1,
        'cat2': cat2,
    }

# ============================================================
# 5. 主题聚类核心逻辑
# ============================================================

def compute_cluster_key(row, features):
    """
    计算聚类的关键标识
    规则2: 不同品类不聚
    规则3: 同品类但判定对象不同要拆
    规则4: 判定标准不同要拆
    """
    project = str(row['project'])
    cat2 = str(row['cat2'])
    objects = features['objects']
    standards = features['standards']

    # 品类 + 主要判定对象 + 主要判定标准
    # 使用cat2作为初始分组，再细化
    obj_key = '+'.join(sorted(objects)) if objects else '未识别对象'
    std_key = '+'.join(sorted(standards)) if standards else '未识别标准'

    return {
        'project': project,
        'cat1': features['cat1'],
        'cat2': cat2,
        'obj_key': obj_key,
        'std_key': std_key,
    }

def is_same_topic(f1, f2, row1, row2):
    """
    判断两个案例是否属于同一主题
    规则5: 同一对象的不同描述角度不拆（如破损+凹陷）
    规则6: 同一问题的追问链不拆
    """
    # 品类必须相同
    if f1['project'] != f2['project']:
        return False

    # 判定对象有交集就可以合并（不同描述角度）
    if f1['objects'] and f2['objects']:
        if not f1['objects'].intersection(f2['objects']):
            return False

    # 标准有交集或标准相近可以合并
    # 破坏类标准可合并: 破损/碎裂/磕碰/凹陷/划痕
    damage_standards = {'破损/碎裂', '磕碰/凹陷', '划痕/磨损', '掉漆'}
    display_standards = {'色斑/显示异常', '亮点/亮斑', '老化', '漏液', '屏生线/横纹', '透图/残影', '闪屏/花屏'}
    repair_standards = {'拆修痕迹', '进灰/异物', '脱胶/溢胶'}
    liquid_standards = {'进液/浸液', '防水标', '发霉/生锈'}

    s1 = f1['standards']
    s2 = f2['standards']

    # 同一大类标准可以合并
    if s1 and s2:
        if s1.intersection(s2):
            return True  # 有交集
        if s1.issubset(damage_standards) and s2.issubset(damage_standards):
            return True  # 同为破坏类外观
        if s1.issubset(display_standards) and s2.issubset(display_standards):
            return True  # 同为显示类
        if s1.issubset(repair_standards) and s2.issubset(repair_standards):
            return True  # 同为拆修类

    # 如果对象相同但标准不同，需要拆（规则4）
    if f1['objects'] == f2['objects'] and f1['standards'] != f2['standards']:
        # 除非是描述角度的补充（规则5）
        return False

    return f1['cat2'] == f2['cat2']


def generate_topic_label(cluster_cases, cluster_features):
    """
    为主题生成可读标签
    """
    project = cluster_features['project']
    cat1 = cluster_features['cat1']
    cat2 = cluster_features['cat2']

    # 收集所有对象和标准
    all_objects = set()
    all_standards = set()
    for f in cluster_features['case_features']:
        all_objects.update(f['objects'])
        all_standards.update(f['standards'])

    obj_str = '、'.join(sorted(all_objects)[:4]) if all_objects else '综合'
    std_str = '、'.join(sorted(all_standards)[:4]) if all_standards else '综合'

    label = f"【{project}】{cat1}-{cat2} | 对象: {obj_str} | 标准: {std_str} | 共{len(cluster_cases)}条"
    return label


# ============================================================
# 6. 执行聚类
# ============================================================

print("\n=== 开始聚类分析 ===")

# 初始化结果
clusters = []  # [(cases_indices, topic_label, features), ...]
unclustered = []  # 无法聚类的案例

# 提取所有有效案例的特征
all_features = {}
for idx in valid_cases:
    row = df.loc[idx]
    features = extract_key_features(row)
    all_features[idx] = features

# 按品类分组
project_groups = defaultdict(list)
for idx in valid_cases:
    project = str(df.loc[idx, 'project'])
    project_groups[project].append(idx)

print(f"品类数: {len(project_groups)}")
for proj, indices in sorted(project_groups.items()):
    print(f"  {proj}: {len(indices)} 条")

# 在每个品类内，按cat2分组作为初始聚类
# 然后在cat2组内，根据对象和标准进一步细分

def cluster_within_project(indices):
    """在同一个品类内进行聚类"""
    sub_clusters = []

    # 第一层：按cat2分组
    cat2_groups = defaultdict(list)
    for idx in indices:
        cat2 = str(df.loc[idx, 'cat2'])
        cat2_groups[cat2].append(idx)

    for cat2, cat2_indices in cat2_groups.items():
        if len(cat2_indices) <= 1:
            # 只有1个案例，也作为一个独立主题
            idx = cat2_indices[0]
            row = df.loc[idx]
            f = all_features[idx]
            label = generate_topic_label([idx], {
                'project': str(row['project']),
                'cat1': f['cat1'],
                'cat2': cat2,
                'case_features': [f]
            })
            sub_clusters.append({
                'indices': [idx],
                'label': label,
                'project': str(row['project']),
                'cat1': f['cat1'],
                'cat2': cat2,
                'objects': f['objects'],
                'standards': f['standards'],
                'size': 1,
                'case_features': [f]
            })
            continue

        # 第二层：按判定对象+标准细分
        # 先按对象分组
        obj_groups = defaultdict(list)
        for idx in cat2_indices:
            f = all_features[idx]
            obj_key = '+'.join(sorted(f['objects'])) if f['objects'] else '未识别对象'
            obj_groups[obj_key].append(idx)

        for obj_key, obj_indices in obj_groups.items():
            if len(obj_indices) <= 1:
                idx = obj_indices[0]
                row = df.loc[idx]
                f = all_features[idx]
                label = generate_topic_label([idx], {
                    'project': str(row['project']),
                    'cat1': f['cat1'],
                    'cat2': cat2,
                    'case_features': [f]
                })
                sub_clusters.append({
                    'indices': [idx],
                    'label': label,
                    'project': str(row['project']),
                    'cat1': f['cat1'],
                    'cat2': cat2,
                    'objects': f['objects'],
                    'standards': f['standards'],
                    'size': 1,
                    'case_features': [f]
                })
                continue

            # 第三层：按判定标准细分
            std_groups = defaultdict(list)
            for idx in obj_indices:
                f = all_features[idx]
                std_key = '+'.join(sorted(f['standards'])) if f['standards'] else '未识别标准'
                std_groups[std_key].append(idx)

            for std_key, std_indices in std_groups.items():
                project_val = str(df.loc[std_indices[0], 'project'])
                cat1_val = all_features[std_indices[0]]['cat1']
                case_fs = [all_features[i] for i in std_indices]

                label = generate_topic_label(std_indices, {
                    'project': project_val,
                    'cat1': cat1_val,
                    'cat2': cat2,
                    'case_features': case_fs
                })

                all_objs = set()
                all_stds = set()
                for f in case_fs:
                    all_objs.update(f['objects'])
                    all_stds.update(f['standards'])

                sub_clusters.append({
                    'indices': std_indices,
                    'label': label,
                    'project': project_val,
                    'cat1': cat1_val,
                    'cat2': cat2,
                    'objects': all_objs,
                    'standards': all_stds,
                    'size': len(std_indices),
                    'case_features': case_fs
                })

    return sub_clusters


# 对每个品类执行聚类
all_clusters = []
for project, indices in sorted(project_groups.items()):
    project_clusters = cluster_within_project(indices)
    all_clusters.extend(project_clusters)
    print(f"\n{project}: {len(project_clusters)} 个主题")

# 按主题大小排序
all_clusters.sort(key=lambda c: c['size'], reverse=True)

# ============================================================
# 7. 输出结果
# ============================================================

print(f"\n=== 聚类结果总览 ===")
print(f"总案例数: {len(df)}")
print(f"噪声案例(排除): {len(noise_cases)}")
print(f"有效案例: {len(valid_cases)}")
print(f"聚类主题数: {len(all_clusters)}")
print(f"最大主题: {all_clusters[0]['size']} 条")

# 打印所有主题概览
print(f"\n=== 所有主题列表 ===")
for i, c in enumerate(all_clusters):
    # 显示主题中前3条案例的核心问题摘要
    summaries = []
    for idx in c['indices'][:3]:
        core = str(df.loc[idx, 'core_issue'])[:80]
        summaries.append(f"[{idx}]{core}")

    print(f"\n--- 主题{i+1}: {c['size']}条 | {c['project']} | {c['cat1']}/{c['cat2']} ---")
    print(f"  对象: {c['objects']}")
    print(f"  标准: {c['standards']}")
    for s in summaries:
        print(f"  {s}")

# 保存详细结果到Excel
output_data = []
for i, c in enumerate(all_clusters):
    for idx in c['indices']:
        row = df.loc[idx]
        output_data.append({
            '主题编号': i + 1,
            '主题标签': c['label'],
            '品类': c['project'],
            '一级分类': c['cat1'],
            '二级分类': c['cat2'],
            '主题对象': '、'.join(sorted(c['objects'])) if c['objects'] else '',
            '主题标准': '、'.join(sorted(c['standards'])) if c['standards'] else '',
            '主题案例数': c['size'],
            '原始索引': idx,
            '会话ID': row['session_id'],
            '型号': row['model'],
            '核心问题': row['core_issue'],
            '判定结果': row['judgment_result'],
            '判定依据': str(row['judgment_basis'])[:500],
            '聊天记录': str(row['chat_log'])[:500],
            '参考话术': str(row['reference_script'])[:300],
        })

output_df = pd.DataFrame(output_data)
output_path = f'data/聚类结果_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
output_df.to_excel(output_path, index=False, engine='openpyxl')
print(f"\n详细结果已保存至: {output_path}")

# 保存主题摘要
summary_data = []
for i, c in enumerate(all_clusters):
    # 收集主题内案例的典型问题
    typical_issues = []
    for idx in c['indices'][:5]:
        typical_issues.append(str(df.loc[idx, 'core_issue'])[:200])

    summary_data.append({
        '主题编号': i + 1,
        '品类': c['project'],
        '一级分类': c['cat1'],
        '二级分类': c['cat2'],
        '判定对象': '、'.join(sorted(c['objects'])) if c['objects'] else '未识别',
        '判定标准': '、'.join(sorted(c['standards'])) if c['standards'] else '未识别',
        '案例数': c['size'],
        '典型问题示例': '\n---\n'.join(typical_issues),
        '案例索引': ', '.join(str(idx) for idx in c['indices']),
    })

summary_df = pd.DataFrame(summary_data)
summary_path = f'data/主题摘要_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
summary_df.to_excel(summary_path, index=False, engine='openpyxl')
print(f"主题摘要已保存至: {summary_path}")

# 保存噪声案例
if noise_cases:
    noise_data = []
    for idx, reason in noise_cases:
        row = df.loc[idx]
        noise_data.append({
            '原始索引': idx,
            '排除原因': reason,
            '品类': row['project'],
            '核心问题': str(row['core_issue'])[:200],
            '聊天记录': str(row['chat_log'])[:300],
            '判定结果': str(row['judgment_result'])[:200],
        })
    noise_df = pd.DataFrame(noise_data)
    noise_path = f'data/噪声排除案例_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    noise_df.to_excel(noise_path, index=False, engine='openpyxl')
    print(f"噪声案例已保存至: {noise_path}")

print("\n=== 聚类完成 ===")
