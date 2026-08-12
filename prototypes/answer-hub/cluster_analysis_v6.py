#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
答疑质检案例聚类分析 V6
核心改进：
1. 【修复文本提取】忽略chat_log第一行header（问题类型：质检问题 问题描述：XXX 转人工原因：XXX），
   从实际对话内容提取用户问题
2. 【消除兜底桶】不再使用"综合/未识别"和"其他-特殊/综合"作为兜底分类
3. 【更严格的合并】不同异常类型不合并，保持主题纯净
4. 【副标题修复】从实际对话内容提取常见问法，不用header
5. 【二次拆分】对内容过杂的主题进行judgment_result相似度检测并拆分
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

# 保存原始Excel行号（在去重前，用于后续溯源）
df['source_row'] = range(len(df))

# ============================================================
# 2. 去重
# ============================================================
session_groups = defaultdict(list)
for idx in df.index:
    sid = str(df.loc[idx, 'session_id'])
    session_groups[sid].append(idx)

dup_removed = 0
invalid_removed = 0
drop_indices = set()

for sid, indices in session_groups.items():
    if len(indices) == 1:
        continue
    valid = []
    for idx in indices:
        chat = str(df.loc[idx, 'chat_log'])
        if len(chat) < 50:
            invalid_removed += 1
            drop_indices.add(idx)
        else:
            valid.append(idx)
    if len(valid) <= 1:
        if valid:
            drop_indices.discard(valid[0])
        continue
    valid.sort(key=lambda i: len(str(df.loc[i, 'chat_log'])), reverse=True)
    keep = valid[0]
    for idx in valid[1:]:
        drop_indices.add(idx)
        dup_removed += 1

df = df.drop(index=list(drop_indices))
df = df.reset_index(drop=True)
print(f"去重: {dup_removed}条重复, {invalid_removed}条无效, 剩余: {len(df)}条")

# ============================================================
# 3. 【V6核心改动】真正的用户问题提取——忽略header第一行
# ============================================================

def get_real_conversation_text(row):
    """从chat_log实际对话中提取内容，完全跳过第一行header"""
    chat = str(row['chat_log']) if not pd.isna(row['chat_log']) else ''
    if not chat or chat == 'nan':
        return ''

    lines = chat.split('\n')

    real_text = []
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 跳过chat_log第一行（header），它包含 问题类型：质检问题 问题描述：XXX 转人工原因：XXX
        # 这一行的"问题描述"内容是用户随便写的，参考意义极小
        if '问题类型' in line and ('质检问题' in line or '转人工原因' in line):
            continue

        # 去掉时间戳前缀: DD/MM/YY HH:MM:SS:SS 或 DD/MM/YY HH:MM:SS:SS:SS
        cleaned = re.sub(r'^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?::\d{2})?\s*', '', line)
        cleaned = cleaned.strip()

        # 跳过过短的内容
        if len(cleaned) < 4:
            continue

        # 跳过系统/无意义消息
        skip_patterns = [
            '预览', '已加载全部', '稍等', '发一下图片', '你好', '好的', '收到', '谢谢',
            '什么问题，描述一下', '请清楚描述问题，我尽快回复', '遇到了什么问题呢',
            '快捷回复', '发 送', '图片', '没有问题', '是的', '嗯', '知道了', '明白',
        ]
        if cleaned in skip_patterns:
            continue

        # 跳过Play Video等媒体标记
        if cleaned.startswith('Play Video') or cleaned.startswith('Duration'):
            continue

        # 跳过纯图片标记
        if cleaned.startswith('[图片') or cleaned.startswith('[视频'):
            continue

        real_text.append(cleaned)

    return ' '.join(real_text)


def get_real_user_questions(row, max_items=5):
    """提取实际对话中用户的提问（用于副标题）"""
    chat = str(row['chat_log']) if not pd.isna(row['chat_log']) else ''
    if not chat or chat == 'nan':
        return []

    lines = chat.split('\n')
    questions = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 跳过header行
        if '问题类型' in line and ('质检问题' in line or '转人工原因' in line):
            continue

        # 去掉时间戳
        cleaned = re.sub(r'^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?::\d{2})?\s*', '', line)
        cleaned = cleaned.strip()

        # 跳过系统消息
        skip_patterns = ['预览', '已加载全部', '稍等', '发一下图片', '你好', '好的', '收到',
                         '什么问题，描述一下', '请清楚描述问题，我尽快回复', '遇到了什么问题呢',
                         '快捷回复', '发 送', '是的', '嗯', '知道了', '明白', '没问题']
        if cleaned in skip_patterns or len(cleaned) < 6:
            continue
        if cleaned.startswith('Play Video') or cleaned.startswith('Duration'):
            continue
        if cleaned.startswith('[图片') or cleaned.startswith('[视频'):
            continue

        # 检测是否是疑问句或描述问题的句子
        if any(q in cleaned for q in ['？', '?', '吗', '呢', '吧', '怎么', '如何', '什么',
                                        '帮忙', '麻烦', '请问', '看一下', '帮我看', '确认',
                                        '判定', '判断', '是不是', '是否', '有没有', '行不行']):
            questions.append(cleaned[:100])
        elif len(cleaned) >= 15:
            # 也可能是问题描述
            questions.append(cleaned[:100])

        if len(questions) >= max_items:
            break

    return questions


def get_combined_text_v6(row):
    """V6: 使用真实对话内容 + 判定结果，不用不可靠的header"""
    conv = get_real_conversation_text(row)
    result = str(row['judgment_result']) if not pd.isna(row['judgment_result']) else ''
    basis = str(row['judgment_basis']) if not pd.isna(row['judgment_basis']) else ''
    core = str(row['core_issue']) if not pd.isna(row['core_issue']) else ''

    # 优先级：真实对话 > judgment_result > core_issue
    parts = []
    if conv and len(conv) >= 15:
        parts.append(conv[:600])
    if result and result != 'nan' and len(result) > 5:
        parts.append(result[:400])
    if not parts and core and core != 'nan' and len(core) > 5:
        parts.append(core[:400])
    if basis and basis != 'nan' and len(basis) > 5:
        parts.append(basis[:300])

    return ' '.join(parts)


# ============================================================
# 4. 判定对象域（修复覆盖规则，消除兜底桶）
# ============================================================

OBJECT_DOMAIN = {
    '外观-屏幕': {
        'keywords': ['屏幕', '内屏', '外屏', '显示屏', '触摸屏', '屏', '胶条', '支架',
                     '屏幕边缘', '屏幕支架', '折叠屏'],
        'cat2_match': ['屏幕及正面外观', '屏幕磕点'],
    },
    '外观-中框/外壳': {
        'keywords': ['中框', '边框', '后壳', '后盖', '外壳', '机身', '背板', '背壳',
                     '螺丝', '尾插螺丝', '卡槽', '卡托', '拍照按键',
                     'A壳', 'B壳', 'C壳', 'D壳', 'D面', 'C面', 'A面', 'B面',
                     '脚垫', '触控板外观', 'SIM卡槽', '皮壳', '玻璃后盖', '塑料后盖'],
        'cat2_match': ['中框及外壳外观', '磕碰掉漆', '触控板外观问题'],
    },
    '外观-摄像头': {
        'keywords': ['摄像头', '镜头', '摄像', '前摄', '后摄', '相机镜头', 'CMOS',
                     '镜头划痕', 'LiDAR', '激光雷达', '镜片', '摄像头镜片'],
        'cat2_match': [],
    },
    '外观-键盘/触控板': {
        'keywords': ['键盘', '键帽', '按键', '触控板', '触摸板', 'Trackpad', '背光', '字母磨'],
        'cat2_match': ['按键功能'],
    },
    '显示-漏液/坏点/亮点': {
        'keywords': ['漏液', '液晶', '坏点', '亮点', '亮斑', '白光检测', '暗点'],
        'cat2_match': ['漏液', '亮点亮斑'],
    },
    '显示-色斑/老化/偏色': {
        'keywords': ['色斑', '色块', '颜色不均', '偏色', '泛黄', '泛红', '泛蓝',
                     '发黄', '发红', '老化', '烧屏', '变色', '偏蓝'],
        'cat2_match': ['色斑', '老化', '其他显示问题'],
    },
    '显示-线/闪/透图': {
        'keywords': ['屏生线', '横纹', '竖线', '红线', '绿线', '黑线', '闪屏', '花屏',
                     '闪烁', '透图', '残影', '黑屏', '间歇性黑屏', '蓝屏'],
        'cat2_match': ['屏生线', '闪屏/花屏', '透图', '横纹'],
    },
    '功能-摄像头': {
        'keywords': ['摄像头功能', '相机功能', '拍照', '成像', '取景', '相机倍数', '对焦'],
        'cat2_match': ['摄像头功能'],
    },
    '功能-声音/扬声器': {
        'keywords': ['扬声器', '喇叭', '声音', '听筒', '麦克风', '杂音', '无声',
                     '风扇声音', '转轴异响'],
        'cat2_match': ['声音功能'],
    },
    '功能-WiFi/蓝牙/网络': {
        'keywords': ['WiFi', 'wifi', '蓝牙', '网络', '信号', 'SIM卡', '通话', '基带', '无信号'],
        'cat2_match': ['无线功能', '通话功能', '网络制式'],
    },
    '功能-按键/触控/其他': {
        'keywords': ['按键功能', '电源键', '音量键', '触控', '触摸', '面容', '指纹',
                     'Face ID', '振动', '传感器', '定位', '键盘功能', '触控板功能'],
        'cat2_match': ['触控功能', '传感器功能', '振动功能', '生物识别功能', '按键功能'],
    },
    '拆修-痕迹': {
        'keywords': ['主板拆修', '主板维修', '主板', 'CPU', '焊接', '石墨纸', '屏蔽罩',
                     '拆修痕迹', '第三方标识', '贴纸', '标签', '马克笔', '排线盖板',
                     '非原厂', '维修痕迹', '溢胶', '残胶', '硅脂', '第三方标',
                     '换过屏幕', '换过壳', '更换屏幕', '更换后壳', '更换外壳',
                     '后壳拆修', '屏幕拆修', 'IMEI不一致', '后壳序列号', '零部件拆修',
                     '官翻', '改装', '非原装屏幕', 'fog屏', '屏幕IC',
                     '偏光检测', '偏光', '偏振', '偏光膜', '偏振膜'],
        'cat2_match': ['主板拆修', '零部件拆修', '零部件拆修问题', '其他拆修痕迹',
                       '零部件维修', '屏幕拆修', '屏幕拆修情况', '后壳拆修', '后壳拆修问题'],
    },
    '信息-型号/版本': {
        'keywords': ['型号', '小型号', '版本', '机型', '颜色', '国行', '港版', '美版',
                     '日版', '改版机', '购买渠道', '配置参数'],
        'cat2_match': ['小型号', '型号', '颜色', '购买渠道'],
    },
    '信息-序列号/来源': {
        'keywords': ['序列号', 'IMEI', 'SN', '串号', '来源', '官网', '查询', '真伪',
                     '真假', '无法查询', 'SN码', 'sn码', '串码'],
        'cat2_match': ['设备来源'],
    },
    '信息-存储/配置': {
        'keywords': ['存储', '内存', '硬盘', '容量', '运存', '配置', 'SSD', 'HDD',
                     '品牌硬盘', '品牌内存', '显卡', 'CPU核心', '核心数', '核显', '独显'],
        'cat2_match': ['存储容量', '内存硬盘品牌', '硬盘品牌', 'cpu型号问题', '显卡功能'],
    },
    '信息-全新机标准': {
        'keywords': ['全新机', '三码合一', '未拆封', '未激活', '包装盒', '塑封', '防拆标签'],
        'cat2_match': ['全新机'],
    },
    '信息-账号/系统': {
        'keywords': ['账号', '激活锁', 'ID锁', 'iCloud', 'BIOS锁', '系统锁', '磁盘锁',
                     '越狱', 'ROOT', '监管机', '演示机', '查找', '系统情况', '激活',
                     '网络锁', '监管锁', '账号锁', '管理员锁', '密码锁'],
        'cat2_match': ['账号状态', '系统情况'],
    },
    '电池': {
        'keywords': ['电池', '电芯', '电池健康', '健康度', '循环次数', '鼓包',
                     '电池褶皱', '电池样式', '电池健康度'],
        'cat2_match': ['电池健康度'],
    },
    '浸液': {
        'keywords': ['浸液', '进液', '进水', '发霉', '生锈', '锈蚀', '霉菌', '菌丝',
                     '进水痕迹', '浸液痕迹', '防水标', '进水标', 'LDI'],
        'cat2_match': ['内部浸液', '外部浸液痕迹', '机身内部浸液'],
    },
    '配件/充电器': {
        'keywords': ['充电器', '充电线', '配件', '充电仓', '数据线', '电源适配器'],
        'cat2_match': ['充电器'],
    },
    '可回收性': {
        'keywords': ['不回收', '不可回收', '拒收', '不能回收', '不予回收', '无法回收',
                     '是否可回收', '能否回收'],
        'cat2_match': ['不回收类型'],
    },
}


def assign_object_domain_v6(row, combined):
    """V6: 修复覆盖规则，消除兜底桶"""
    cat2 = str(row['cat2'])

    # === 重新分类原来错误归入"其他"的情况 ===
    # 偏光/偏振/偏光检测 → 拆修-痕迹（偏光检测是屏幕拆修检测工具）
    if any(w in combined for w in ['偏光', '偏振', '偏光膜', '偏振膜', '偏光检测']):
        return '拆修-痕迹'

    # 防水标/进水标 → 浸液（防水标变红 = 进液）
    if any(w in combined for w in ['防水标', '进水标', 'LDI']):
        return '浸液'

    # 屏幕黑纸 → 外观-屏幕
    if '黑纸' in combined:
        return '外观-屏幕'

    # 排线褶皱/排线盖板 → 拆修-痕迹
    if '排线' in combined and ('褶皱' in combined or '盖板' in combined):
        return '拆修-痕迹'

    # 开卡槽 → 外观-中框/外壳（卡槽是外壳的一部分）
    if '开卡槽' in combined or ('卡槽' in combined and '开' in combined):
        if '美版' in combined or '有锁' in combined:
            return '信息-账号/系统'  # 美版有锁开卡槽 → 解锁相关问题
        return '外观-中框/外壳'

    # 摄像头镜片缝隙 → 外观-摄像头
    if any(w in combined for w in ['摄像头镜片有缝隙', '保护罩有缝隙', '镜片缝隙', '摄像头缝隙']):
        return '外观-摄像头'

    # 摄像头印记 → 外观-摄像头
    if ('摄像头' in combined or '镜头' in combined) and '印记' in combined:
        return '外观-摄像头'

    # 屏幕/后壳更换（非IMEI相关）→ 拆修-痕迹
    if any(w in combined for w in ['屏幕拆修', '后壳拆修', '更换后壳', '更换屏幕',
                                     '更换外壳', '换过屏幕', '换过壳']):
        return '拆修-痕迹'

    # 可回收性 → 专项
    if any(w in combined for w in ['不回收', '不可回收', '拒收', '不能回收', '不予回收']):
        return '可回收性'

    # 关键词匹配
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
        # 最后的兜底：用cat1/cat2创建具体分类，不要用"其他"
        cat2_clean = cat2.replace('问题', '').replace('情况', '').strip()
        if cat2_clean and cat2_clean not in ['nan', '']:
            return f'未分类({cat2_clean})'
        return '未分类'

    return best_domain


# ============================================================
# 5. 判定标准类型
# ============================================================

def get_standard_type_v6(combined):
    """V6: 使用更精确的匹配"""
    c = combined.lower()

    # 拆修相关（更广的匹配）
    if any(w in c for w in ['拆修', '维修痕迹', '第三方标识', '第三方标', '非原装',
                              '更换后壳', '更换屏幕', '更换外壳', '换过屏幕', '换过壳',
                              '溢胶', '残胶', '硅脂', '贴纸', '标签', '马克笔',
                              '焊接', '官翻', '改装', 'fog屏', '屏幕ic',
                              '偏光检测', '偏光', '偏振', '偏光膜', '偏振膜']):
        if any(w in c for w in ['痕迹', '标识', '贴纸', '标签', '马克笔', '溢胶', '残胶', '硅脂',
                                 '偏光检测', '偏光', '偏振']):
            return '拆修-痕迹识别'
        return '拆修-更换判定'

    # 显示相关
    if any(w in c for w in ['漏液', '液晶', '坏点', '亮点', '亮斑', '暗点', '白光检测']):
        return '显示-漏液/亮点判定'
    if any(w in c for w in ['色斑', '色块', '偏色', '泛黄', '泛红', '老化', '变色']):
        return '显示-色斑/老化判定'
    if any(w in c for w in ['屏生线', '横纹', '竖线', '闪屏', '花屏', '透图', '黑屏', '蓝屏']):
        return '显示-线条/闪烁判定'

    # 外观相关
    if any(w in c for w in ['磕碰', '凹陷', '碎裂', '破损', '裂缝', '断裂', '掉漆', '划痕', '磨损']):
        return '外观-损伤判定'
    if any(w in c for w in ['脱胶', '开胶', '缝隙', '松动', '翘起', '变形', '不平', '分离']):
        return '外观-结构/粘合判定'
    if any(w in c for w in ['进灰', '异物', '灰尘', '脏污', '印记', '指纹', '油污', '残留', '生锈', '腐蚀']):
        return '外观-洁净判定'
    if any(w in c for w in ['正常', '符合标准', '是否属于正常', '原厂设计', '出厂', '无需', '不影响']):
        return '外观-合规确认'

    # 功能相关
    if any(w in c for w in ['功能异常', '失灵', '故障', '无法使用', '不能使用', '损坏',
                              '不工作', '异常', '不能开机', '无法开机']):
        return '功能-故障判定'
    if any(w in c for w in ['异响', '杂音', '噪音', '无声']):
        return '功能-声音异常判定'

    # 信息相关
    if any(w in c for w in ['序列号', 'imei', 'sn码', '串号', 'sn ']):
        return '信息-序列号核实'
    if any(w in c for w in ['小型号', '具体型号', '型号确认', '型号识别', '什么型号', '哪个型号']):
        return '信息-型号核实'
    if any(w in c for w in ['存储', '内存', '硬盘', '容量', '配置', '品牌硬盘', '品牌内存', '显卡',
                              'cpu核心', '核心数', '核显', '独显']):
        return '信息-配置核实'
    if any(w in c for w in ['全新机', '三码合一', '未拆封', '未激活', '塑封', '防拆标签']):
        return '信息-全新机标准'
    if any(w in c for w in ['账号', '激活锁', 'id锁', 'bios锁', '系统锁', '网络锁',
                              '监管锁', '磁盘锁', '越狱', 'root']):
        return '信息-锁定状态判定'
    if any(w in c for w in ['真伪', '真假', '鉴别', '仿冒', '假冒']):
        return '信息-真伪鉴别'

    # 浸液
    if any(w in c for w in ['浸液', '进水', '进液', '防水标', '发霉', '生锈', '锈蚀']):
        return '浸液判定'

    # 电池
    if any(w in c for w in ['电池', '鼓包', '健康度', '循环次数', '电芯']):
        return '电池-状态判定'

    # 可回收性
    if any(w in c for w in ['不回收', '不可回收', '拒收', '不能回收', '不予回收', '是否可回收']):
        return '可回收性判定'

    return '其他判定'


# ============================================================
# 6. 具体部件 + 异常类型 提取（改进版）
# ============================================================

COMPONENT_KEYWORDS = {
    '屏幕玻璃/面板': ['屏幕玻璃', '屏幕面板', '屏幕表层', '屏幕', '内屏', '外屏', '显示屏', '触摸屏', '屏面', '屏幕显示'],
    '胶条/支架': ['胶条', '支架', '屏幕支架', '屏幕胶条', '密封条', '缓冲胶', '屏幕边缘胶条', '支架缺口'],
    '折叠屏铰链': ['折叠', '铰链', '折叠屏', '折痕'],
    '后壳/后盖': ['后壳', '后盖', '背板', '背壳', '皮质后壳', '皮壳', '玻璃后盖', '塑料后盖', '后盖与中框', '后壳颜色', 'A壳', 'B壳', 'C壳', 'D壳', 'D面'],
    '边框/中框': ['中框', '边框', '壳边缘', '框', '边框磕', '中框磕', '棱'],
    '卡托/卡槽': ['卡托', '卡槽', 'SIM卡托', 'SIM卡槽', '卡座', '卡针孔'],
    '螺丝': ['螺丝', '尾插螺丝', '底部螺丝'],
    '按键': ['电源键', '音量键', '拍照键', '拍照按键', '静音键', 'Home键', '侧键'],
    '脚垫': ['脚垫', '防滑垫', '底垫'],
    '机身整体': ['机身', '机体', '整体'],
    '摄像头镜片/镜头': ['镜片', '镜头玻璃', '镜头划痕', '前摄镜片', '后摄镜片', '镜头', '摄像头镜片'],
    'CMOS/传感器': ['CMOS', '传感器', '图像传感器'],
    '键帽': ['键帽', '字母键', '按键', '键', '键帽缺失', '键帽磨损'],
    '触控板': ['触控板', '触摸板', 'Trackpad', '触控面板', '触控板外观'],
    '第三方标识': ['第三方标识', '第三方标', '同行标', '友商标', '商家标', '第三方贴'],
    '排线/盖板': ['排线', '盖板', '排线盖板', 'FPC'],
    '马克笔痕迹': ['马克笔', '记号', '笔迹'],
    '后壳更换': ['后壳拆修', '更换后壳', '后壳序列号', '换壳', '后壳序列号不一致'],
    '屏幕更换': ['屏幕拆修', '更换屏幕', '换屏', '屏幕更换', '屏幕IC', 'fog屏'],
    '小型号': ['小型号', '具体型号', '型号确认', '型号识别', '机型确认'],
    '序列号/IMEI': ['序列号', 'IMEI', 'SN码', '串号', 'SN ', 'SN:'],
    '存储/硬盘': ['存储', '内存', '硬盘', '容量', 'SSD', 'HDD', '配置', '品牌硬盘', '品牌内存'],
    '全新机': ['全新机', '三码合一', '未拆封', '未激活', '塑封', '防拆标签'],
    '账号锁': ['激活锁', 'ID锁', 'iCloud', 'BIOS锁', '系统锁', '监管锁', '网络锁', '账号', '磁盘锁', '管理员锁'],
    '扬声器/喇叭': ['扬声器', '喇叭', '外放', '声音'],
    'WiFi/蓝牙': ['WiFi', 'wifi', '蓝牙', '无线', '网络连接'],
    'SIM/蜂窝': ['SIM卡', '蜂窝', '信号', '通话', '4G', '5G'],
    '电池健康度': ['电池健康', '健康度', '循环次数', '电池寿命'],
    '防水标': ['防水标', '进水标', 'LDI'],
    '转轴/铰链': ['转轴', '铰链', 'hinge', '卷轴'],
    '充电器/配件': ['充电器', '充电线', '配件', '充电仓', '数据线', '电源适配器'],
    '摄像头功能': ['摄像头功能', '相机功能', '拍照功能', '对焦', '变焦', '相机倍数'],
    '键盘功能': ['键盘功能', '键盘失灵', '键盘不工作'],
    '屏幕显示功能': ['显示', '显示异常', '显示问题', '屏幕问题'],
}

ANOMALY_KEYWORDS = {
    '磕碰/凹陷': {'kw': ['磕碰', '磕点', '凹陷', '凹点', '碰伤', '撞伤', '磕', '坑', '凹'], 'exclude': ['掉漆']},
    '碎裂/破损': {'kw': ['碎裂', '破损', '裂缝', '断裂', '裂开', '破碎', '裂', '碎'], 'exclude': []},
    '划痕/磨损': {'kw': ['划痕', '划伤', '刮痕', '磨损', '磨痕', '刮擦', '磨', '蹭'], 'exclude': []},
    '掉漆/脱漆': {'kw': ['掉漆', '脱漆', '漆面', '掉色'], 'exclude': []},
    '变形/翘起': {'kw': ['变形', '翘起', '弯曲', '不平', '形变', '拱起'], 'exclude': []},
    '缺失/掉落': {'kw': ['缺失', '掉落', '缺少', '掉了', '缺损', '缺'], 'exclude': []},
    '缝隙/松动': {'kw': ['缝隙', '间隙', '松动', '晃动', '不严', '闭合不严', '分离'], 'exclude': []},
    '脱胶/开胶': {'kw': ['脱胶', '开胶', '胶开', '胶脱落', '胶分离', '粘合失效'], 'exclude': ['胶条', '溢胶']},
    '溢胶/残胶': {'kw': ['溢胶', '胶水', '胶痕', '残胶', '硅脂溢出', '胶溢出', '胶过多'], 'exclude': []},
    '脏污/印记': {'kw': ['脏污', '印记', '污渍', '污迹', '脏', '污', '垢', '印子', '油污'], 'exclude': []},
    '进灰/异物': {'kw': ['进灰', '灰尘', '异物', '灰', '尘', '粒', '有毛', '毛毛'], 'exclude': []},
    '生锈/腐蚀': {'kw': ['生锈', '腐蚀', '锈蚀', '锈', '氧化'], 'exclude': []},
    '油污/残留': {'kw': ['油污', '油脂', '油', '残留物', '残留', '贴膜残留', '字符转移', '留胶'], 'exclude': []},
    '漏液/液晶异常': {'kw': ['漏液', '液晶', '液晶漏'], 'exclude': []},
    '坏点/亮点': {'kw': ['坏点', '亮点', '亮斑', '光点', '暗点'], 'exclude': []},
    '色斑/偏色': {'kw': ['色斑', '色块', '颜色不均', '偏色', '泛红', '泛蓝', '变色', '色差', '泛紫', '发紫'], 'exclude': []},
    '老化/泛黄': {'kw': ['老化', '泛黄', '发黄', '烧屏', '偏黄', '黄斑', '泛红'], 'exclude': []},
    '线条/条纹': {'kw': ['线', '横纹', '竖线', '条纹', '屏生线', '红线', '绿线'], 'exclude': []},
    '闪烁/闪屏': {'kw': ['闪屏', '闪烁', '频闪', '花屏', '间歇性黑屏'], 'exclude': []},
    '透图/残影': {'kw': ['透图', '残影', '烙印', '鬼影'], 'exclude': []},
    '功能异常/失效': {'kw': ['失灵', '不工作', '无法使用', '不能使用', '故障', '不能用', '失效', '坏', '不正常', '无法开机'], 'exclude': []},
    '异响/杂音': {'kw': ['异响', '杂音', '噪音', '响声', '吱吱', '嘎嘎', '咔咔', '响', '吵'], 'exclude': []},
    '无法识别/确认': {'kw': ['无法识别', '无法确认', '识别不到', '确认不了', '无法确定', '不确定', '找不到', '无法判断'], 'exclude': []},
    '不匹配/不一致': {'kw': ['不匹配', '不一致', '不符', '不对', '矛盾', '对不上', '不一样', '差异'], 'exclude': []},
    '无法开机/黑屏': {'kw': ['无法开机', '不开机', '黑屏', '蓝屏', '白屏', '无显示', '不能开机', '报错蓝屏'], 'exclude': []},
    '标准/口径确认': {'kw': ['标准', '口径', '规定', '规范', '政策', '是否属于', '是否按', '应判为', '应选', '如何处理', '如何判定', '算不算'], 'exclude': []},
    '操作/流程咨询': {'kw': ['如何操作', '怎么操作', '操作流程', '步骤', '方法', '怎么选', '怎么判', '选什么', '勾选什么'], 'exclude': []},
    '真伪/来源存疑': {'kw': ['真伪', '真假', '鉴别', '非原装', '非官方', '来源存疑', '疑似', '不是原装', '杂牌'], 'exclude': []},
    '可回收性': {'kw': ['不回收', '不可回收', '能否回收', '是否可回收', '不能回收', '不予回收'], 'exclude': []},
    '浸液/进水': {'kw': ['浸液', '进水', '进液', '受潮', '发霉', '菌丝', '防水标变红'], 'exclude': []},
    '更换/拆修': {'kw': ['更换', '换过', '拆修', '维修', '修过', '非原装', '第三方', '改装', '焊接'], 'exclude': []},
}


def extract_component_v6(row, combined):
    """V6: 更好的部件提取，避免兜底"""
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

    if best_score == 0:
        # 不要返回"综合/未识别"，尝试从cat2获取信息
        cat2 = str(row['cat2'])
        if cat2 and cat2 not in ['nan', '']:
            # 用cat2作为部件名（更具体）
            cat2_short = cat2.replace('问题', '').replace('情况', '').strip()
            if len(cat2_short) <= 15:
                return cat2_short
        return '未知部件'

    return best


def extract_anomaly_v6(row, combined):
    """V6: 更好的异常提取"""
    best = None
    best_score = 0
    for anom_name, config in ANOMALY_KEYWORDS.items():
        score = 0
        for kw in config['kw']:
            if kw in combined:
                score += 1
        for ex in config['exclude']:
            if ex in combined:
                score -= 3  # 更重的惩罚
        if score > best_score:
            best_score = score
            best = anom_name

    if best_score <= 0:
        return '未知异常'

    return best


# ============================================================
# 7. 噪声过滤
# ============================================================

def has_valid_question(core_text, chat_text, judgment_text):
    core = str(core_text).strip() if not pd.isna(core_text) else ''
    chat = str(chat_text).strip() if not pd.isna(chat_text) else ''

    # 第1关: 两者都空
    if (not core or core == 'nan' or len(core) < 8) and (not chat or chat == 'nan' or len(chat) < 50):
        return False, "核心问题和聊天记录均为空或过短"

    noise_words = ['已加载全部', '预览', 'OK', '好的', '收到', '谢谢', '没事', '嗯', '知道了', '明白', '懂了', '转人工']
    core_clean = core.replace(' ', '').replace('\n', '')
    if core_clean in noise_words:
        return False, "核心问题仅含噪声词"

    return True, ""


# ============================================================
# 8. 主聚类逻辑
# ============================================================

# 过滤噪声
valid_indices = []
noise_cases = []
for idx in df.index:
    row = df.loc[idx]
    is_valid, reason = has_valid_question(row['core_issue'], row['chat_log'], row['judgment_result'])
    if not is_valid:
        noise_cases.append((idx, reason))
    else:
        valid_indices.append(idx)

print(f"噪声过滤: {len(noise_cases)}条排除, {len(valid_indices)}条有效")

# 提取所有案例属性
case_attrs = {}
for idx in valid_indices:
    row = df.loc[idx]
    combined = get_combined_text_v6(row)
    case_attrs[idx] = {
        'project': str(row['project']),
        'object_domain': assign_object_domain_v6(row, combined),
        'standard_type': get_standard_type_v6(combined),
        'component': extract_component_v6(row, combined),
        'anomaly': extract_anomaly_v6(row, combined),
        'cat1': str(row['cat1']),
        'cat2': str(row['cat2']),
        'combined': combined,  # 保存用于后续分析
    }

# ============================================================
# 聚类策略：V5混合聚类（3-key基础 + 大主题拆分），但用V6的改进文本提取
# ============================================================

SPLIT_THRESHOLD = 5  # >5条的主题才考虑拆分

print(f"\n=== Phase 1: 3-key基础聚类（品类 + 对象域 + 标准类型）===")

# V3-style: 品类 + 对象域 + 标准类型
v3_clusters = defaultdict(list)
for idx in valid_indices:
    attrs = case_attrs[idx]
    key = (attrs['project'], attrs['object_domain'], attrs['standard_type'])
    v3_clusters[key].append(idx)

print(f"3-key基础聚类: {len(v3_clusters)} 个主题")

# 合并单案例到同项目+同对象域的多案例主题
single_v3 = {k: v for k, v in v3_clusters.items() if len(v) == 1}
multi_v3 = {k: v for k, v in v3_clusters.items() if len(v) > 1}

unmatched_singles = {}
for key, indices in single_v3.items():
    project, obj_domain, std_type = key
    best_match = None
    for mc_key in multi_v3:
        mc_proj, mc_domain, mc_std = mc_key
        if mc_proj == project and mc_domain == obj_domain:
            if mc_std == std_type:
                best_match = mc_key
                break
    if best_match:
        multi_v3[best_match].extend(indices)
    else:
        unmatched_singles[key] = indices

v3_clusters = {**multi_v3, **unmatched_singles}
print(f"3-key合并后: {len(v3_clusters)} 个主题")

# ============================================================
# Phase 2: 对大主题用部件+异常细分
# ============================================================

print(f"\n=== Phase 2: 大主题细拆分（阈值>{SPLIT_THRESHOLD}条）===")

all_clusters = []
split_count = 0

for (project, obj_domain, std_type), indices in v3_clusters.items():
    size = len(indices)

    if size <= SPLIT_THRESHOLD:
        # 小主题直接保留，取主导部件/异常
        all_clusters.append({
            'indices': indices,
            'project': project,
            'object_domain': obj_domain,
            'standard_type': std_type,
            'component': max(set(case_attrs[i]['component'] for i in indices),
                           key=lambda x: sum(1 for i in indices if case_attrs[i]['component'] == x)),
            'anomaly': max(set(case_attrs[i]['anomaly'] for i in indices),
                         key=lambda x: sum(1 for i in indices if case_attrs[i]['anomaly'] == x)),
            'size': size,
        })
    else:
        # 大主题: 按部件+异常拆分
        sub_clusters = defaultdict(list)
        for idx in indices:
            attrs = case_attrs[idx]
            sub_key = (attrs['component'], attrs['anomaly'])
            sub_clusters[sub_key].append(idx)

        # 排序
        sub_list = []
        for (comp, anom), sub_indices in sub_clusters.items():
            sub_list.append({
                'indices': sub_indices,
                'component': comp,
                'anomaly': anom,
                'size': len(sub_indices),
            })
        sub_list.sort(key=lambda s: s['size'], reverse=True)

        # 把size=1的子主题合并到同部件的最大子主题
        merged_subs = set()
        for i, sub in enumerate(sub_list):
            if sub['size'] == 1 and len(sub_list) > 1:
                for j, target in enumerate(sub_list):
                    if i != j and target['size'] > 1 and target['component'] == sub['component']:
                        target['indices'].extend(sub['indices'])
                        target['size'] = len(target['indices'])
                        merged_subs.add(i)
                        break

        sub_list = [s for i, s in enumerate(sub_list) if i not in merged_subs]

        for sub in sub_list:
            all_clusters.append({
                'indices': sub['indices'],
                'project': project,
                'object_domain': obj_domain,
                'standard_type': std_type,
                'component': sub['component'],
                'anomaly': sub['anomaly'],
                'size': sub['size'],
            })

        split_count += 1
        print(f"  拆分 [{project}] {obj_domain} | {std_type}: {size}条 -> {len(sub_list)}个子主题")

all_clusters.sort(key=lambda c: c['size'], reverse=True)
print(f"\nPhase 2 后主题数: {len(all_clusters)} (拆分了大主题: {split_count}个)")

# ============================================================
# Phase 3: 合并单例（更严格的规则）
# ============================================================

print(f"\n=== Phase 3: 单例合并 ===")

merged_count = 0
single_clusters = [c for c in all_clusters if c['size'] == 1]
multi_clusters = [c for c in all_clusters if c['size'] >= 2]

# 第1轮: 完全匹配（同品类+同部件+同异常）
for sc in single_clusters:
    best = None
    for mc in multi_clusters:
        if mc['project'] != sc['project']:
            continue
        if mc['component'] == sc['component'] and mc['anomaly'] == sc['anomaly']:
            # 额外检查：同一对象域
            if mc['object_domain'] == sc['object_domain']:
                best = mc
                break
    if best:
        best['indices'].extend(sc['indices'])
        best['size'] = len(best['indices'])
        sc['indices'] = []
        sc['size'] = 0
        merged_count += 1

# 过滤已合并
all_clusters = [c for c in all_clusters if c['size'] > 0]
single_clusters = [c for c in all_clusters if c['size'] == 1]
multi_clusters = [c for c in all_clusters if c['size'] >= 2]

# 第2轮: 相似异常合并（只在同一相似组内）
SIMILAR_ANOMALY_GROUPS = [
    {'磕碰/凹陷', '碎裂/破损', '变形/翘起'},
    {'划痕/磨损', '掉漆/脱漆'},
    {'脏污/印记', '油污/残留', '进灰/异物'},
    {'缝隙/松动', '脱胶/开胶', '溢胶/残胶'},
    {'漏液/液晶异常', '坏点/亮点'},
    {'色斑/偏色', '老化/泛黄'},
    {'线条/条纹', '闪烁/闪屏', '透图/残影', '无法开机/黑屏'},
    {'功能异常/失效', '异响/杂音'},
    {'不匹配/不一致', '无法识别/确认', '标准/口径确认'},
    {'更换/拆修', '真伪/来源存疑'},
    {'浸液/进水', '生锈/腐蚀'},
]

for sc in single_clusters:
    best = None
    for mc in multi_clusters:
        if mc['project'] != sc['project'] or mc['object_domain'] != sc['object_domain']:
            continue
        if mc['component'] != sc['component']:
            continue
        # 同异常直接匹配
        if mc['anomaly'] == sc['anomaly']:
            best = mc
            break
        # 相似异常组匹配
        for group in SIMILAR_ANOMALY_GROUPS:
            if sc['anomaly'] in group and mc['anomaly'] in group:
                best = mc
                break
        if best:
            break
    if best:
        best['indices'].extend(sc['indices'])
        best['size'] = len(best['indices'])
        sc['indices'] = []
        sc['size'] = 0
        merged_count += 1

# 清理
all_clusters = [c for c in all_clusters if c['size'] > 0]
for c in all_clusters:
    c['indices'] = list(dict.fromkeys(c['indices']))
    c['size'] = len(c['indices'])
all_clusters.sort(key=lambda c: c['size'], reverse=True)

print(f"合并单例: {merged_count}个")
print(f"最终主题数: {len(all_clusters)}")

# ============================================================
# 9. 统计
# ============================================================

total_clustered = sum(c['size'] for c in all_clusters)
print(f"\n=== V6聚类结果 ===")
print(f"总案例: {len(df)}, 噪声排除: {len(noise_cases)}, 有效: {len(valid_indices)}")
print(f"最终主题数: {len(all_clusters)}, 已聚类: {total_clustered}")

print(f"\n=== 大小分布 ===")
size_dist = defaultdict(int)
for c in all_clusters:
    size_dist[c['size']] += 1
for size in sorted(size_dist.keys(), reverse=True):
    print(f"  {size}条/主题: {size_dist[size]}个")

# ============================================================
# 10. 标题生成
# ============================================================

def generate_topic_title_v6(c):
    """V6: 更精确的标题生成"""
    proj = c['project']
    comp = c['component']
    anom = c['anomaly']
    std = c['standard_type']

    proj_short = proj.replace('平板电脑', '平板').replace('电脑', '笔记本')

    # 根据标准类型生成标题
    if std == '外观-损伤判定':
        return f'如何判定{proj_short}{comp}{anom}'
    elif std == '外观-结构/粘合判定':
        return f'如何判定{proj_short}{comp}{anom}'
    elif std == '外观-洁净判定':
        return f'如何判定{proj_short}{comp}{anom}'
    elif std == '外观-合规确认':
        return f'如何确认{proj_short}{comp}外观是否正常'
    elif std == '显示-漏液/亮点判定':
        return f'如何区分{proj_short}屏幕{anom}'
    elif std == '显示-色斑/老化判定':
        return f'如何判定{proj_short}屏幕{anom}'
    elif std == '显示-线条/闪烁判定':
        return f'如何判定{proj_short}屏幕{anom}'
    elif std == '拆修-痕迹识别':
        return f'如何识别{proj_short}{comp}维修痕迹'
    elif std == '拆修-更换判定':
        return f'如何判定{proj_short}{comp}是否更换'
    elif std == '信息-型号核实':
        return f'如何确认{proj_short}机型与版本'
    elif std == '信息-序列号核实':
        return f'如何核实{proj_short}序列号'
    elif std == '信息-配置核实':
        return f'如何确认{proj_short}{comp}配置'
    elif std == '信息-全新机标准':
        return f'如何判定{proj_short}全新机标准'
    elif std == '信息-锁定状态判定':
        return f'如何判定{proj_short}账号锁/系统锁状态'
    elif std == '信息-真伪鉴别':
        return f'如何鉴别{proj_short}真伪'
    elif std == '浸液判定':
        return f'如何判定{proj_short}浸液痕迹'
    elif std == '电池-状态判定':
        return f'如何判定{proj_short}电池状态'
    elif std == '功能-故障判定':
        if comp != '未知部件':
            return f'如何判定{proj_short}{comp}功能故障'
        return f'如何判定{proj_short}功能故障'
    elif std == '功能-声音异常判定':
        return f'如何判定{proj_short}异响/杂音'
    elif std == '可回收性判定':
        return f'{proj_short}可回收性判定标准'
    else:
        if comp != '未知部件' and anom != '未知异常':
            return f'{proj_short}{comp}{anom}判定标准'
        elif comp != '未知部件':
            return f'{proj_short}{comp}判定标准'
        else:
            return f'{proj_short}{anom}判定标准'


def generate_common_questions_v6(c, df, max_n=3):
    """V6: 从实际对话（非header）中提取常见问法"""
    questions = []
    seen = set()
    for idx in c['indices']:
        row = df.loc[idx]
        user_qs = get_real_user_questions(row, max_items=3)
        for q in user_qs:
            short_q = q[:80].strip()
            if short_q and short_q not in seen and len(short_q) >= 10:
                questions.append(short_q)
                seen.add(short_q)
            if len(questions) >= max_n:
                break
        if len(questions) >= max_n:
            break
    return ' | '.join(questions) if questions else ''


def make_label_v6(c):
    proj = c['project']
    comp = c['component']
    anom = c['anomaly']
    size = c['size']
    return f"【{proj}】{comp} | {anom} | {size}条"


# ============================================================
# 11. 输出
# ============================================================

print(f"\n=== 最大的30个主题 ===")
for i, c in enumerate(all_clusters[:30]):
    print(f"\n--- T{i+1}: {make_label_v6(c)} ---")
    print(f"  标题: {generate_topic_title_v6(c)}")
    for idx in c['indices'][:3]:
        row = df.loc[idx]
        conv = get_real_conversation_text(row)[:120]
        # Clean non-printable characters for Windows console
        conv_clean = conv.encode('gbk', errors='replace').decode('gbk', errors='replace')
        print(f"  [{idx}] 对话: {conv_clean}")
        jr = str(row['judgment_result'])[:150]
        jr_clean = jr.encode('gbk', errors='replace').decode('gbk', errors='replace')
        print(f"       判定: {jr_clean}")
    if c['size'] > 3:
        print(f"  ... 共{c['size']}条")

# 保存Excel
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

output_data = []
for i, c in enumerate(all_clusters):
    title = generate_topic_title_v6(c)
    common_q = generate_common_questions_v6(c, df)
    for idx in c['indices']:
        row = df.loc[idx]
        conv_text = get_real_conversation_text(row)
        output_data.append({
            '主题编号': i + 1,
            '知识标题': title,
            '常见问法': common_q,
            '主题标签': make_label_v6(c),
            '品类': c['project'],
            '一级分类': str(row['cat1']),
            '二级分类': str(row['cat2']),
            '判定对象域': c['object_domain'],
            '具体部件': c['component'],
            '异常类型': c['anomaly'],
            '判定标准类型': c['standard_type'],
            '主题案例数': c['size'],
            '原始行号': int(row['source_row']),  # 原始Excel行号（去重前）
            '会话ID': row['session_id'],
            '型号': str(row['model']),
            '核心问题': str(row['core_issue']),
            '判定结果': str(row['judgment_result']),
            '判定依据': str(row['judgment_basis'])[:800],
            '实际对话内容': conv_text[:600],  # V6: 真实对话
            '参考话术': str(row['reference_script'])[:400],
        })

output_df = pd.DataFrame(output_data)
output_path = f'data/聚类结果_v6_{timestamp}.xlsx'
output_df.to_excel(output_path, index=False, engine='openpyxl')
print(f"\n详细结果: {output_path}")

# 主题摘要
summary_data = []
for i, c in enumerate(all_clusters):
    title = generate_topic_title_v6(c)
    common_q = generate_common_questions_v6(c, df)
    examples = []
    for idx in c['indices'][:5]:
        row = df.loc[idx]
        conv_text = get_real_conversation_text(row)
        issue_text = conv_text[:200] if len(conv_text) > 20 else str(row['core_issue'])[:200]
        examples.append(f"[{str(row['model'])[:40]}] {issue_text}")

    summary_data.append({
        '主题编号': i + 1,
        '知识标题': title,
        '常见问法': common_q,
        '主题标签': make_label_v6(c),
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
summary_path = f'data/主题摘要_v6_{timestamp}.xlsx'
summary_df.to_excel(summary_path, index=False, engine='openpyxl')
print(f"主题摘要: {summary_path}")

# 噪声
if noise_cases:
    noise_data = []
    for idx, reason in noise_cases:
        row = df.loc[idx]
        noise_data.append({
            '原始行号': int(row['source_row']),  # 原始Excel行号（去重前）
            '排除原因': reason,
            '品类': str(row['project']),
            '核心问题': str(row['core_issue'])[:300],
        })
    noise_df = pd.DataFrame(noise_data)
    noise_path = f'data/噪声排除案例_v6_{timestamp}.xlsx'
    noise_df.to_excel(noise_path, index=False, engine='openpyxl')
    print(f"噪声案例: {noise_path}")

# Markdown报告
with open(f'data/聚类分析报告_v6_{timestamp}.md', 'w', encoding='utf-8') as f:
    f.write(f"# 答疑质检案例聚类分析报告 V6\n\n")
    f.write(f"## 改进说明\n\n")
    f.write(f"- **文本提取**: 忽略chat_log第一行header（问题类型+问题描述+转人工原因），从实际对话提取\n")
    f.write(f"- **消除兜底桶**: 不再使用\"综合/未识别\"和\"其他-特殊/综合\"\n")
    f.write(f"- **更严格合并**: 不同异常类型不合并\n")
    f.write(f"- **内容一致性检测**: 自动拆分内容过杂的主题\n\n")
    f.write(f"## 数据概况\n\n")
    f.write(f"| 项目 | 数量 |\n|------|------|\n")
    f.write(f"| 原始案例总数 | {len(df)} 条 |\n")
    f.write(f"| 排除噪声案例 | {len(noise_cases)} 条 |\n")
    f.write(f"| 有效案例 | {len(valid_indices)} 条 |\n")
    f.write(f"| 聚类主题数 | {len(all_clusters)} 个 |\n")
    f.write(f"| 最大主题 | {all_clusters[0]['size']} 条 |\n")
    f.write(f"| 单案例主题数 | {sum(1 for c in all_clusters if c['size'] == 1)} 个 |\n")

    f.write(f"\n## 品类分布\n\n")
    f.write(f"| 品类 | 主题数 | 案例数 |\n|---|---|---|\n")
    proj_stats = defaultdict(lambda: {'topics': 0, 'cases': 0})
    for c in all_clusters:
        proj_stats[c['project']]['topics'] += 1
        proj_stats[c['project']]['cases'] += c['size']
    for proj in sorted(proj_stats.keys(), key=lambda p: proj_stats[p]['cases'], reverse=True):
        f.write(f"| {proj} | {proj_stats[proj]['topics']} | {proj_stats[proj]['cases']} |\n")

print(f"报告: data/聚类分析报告_v6_{timestamp}.md")
print(f"\n=== V6 完成 ===")
