from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
import hashlib

from .product_taxonomy import canonical_product_name


@dataclass(frozen=True)
class TerminologyEntry:
    term: str
    category: str
    aliases: tuple[str, ...]
    meaning: str
    usage: str
    product_categories: tuple[str, ...] = ()


class TerminologyError(RuntimeError):
    """Raised when the shared terminology dictionary is unavailable or invalid."""


_RAW_TERMINOLOGY_ENTRIES: tuple[TerminologyEntry, ...] = (
    TerminologyEntry(
        term="偏光检测",
        category="检测方法",
        aliases=("偏光", "偏振", "偏光工具"),
        meaning="屏幕检测工具或检测动作",
        usage="通常用于屏幕拆修或显示核验；只有聊天或图片明确指向真实屏幕异常时，才按显示问题处理。",
    ),
    TerminologyEntry(
        term="红线检测",
        category="检测方法",
        aliases=("红线", "红线工具", "黑边检测"),
        meaning="屏幕边界检测工具或检测方法",
        usage="通常用于检测屏幕边界或黑边，不要直接等同于屏幕物理红线；只有实际聊天或图片明确提到线条本体时，才按显示问题处理。",
    ),
    TerminologyEntry(
        term="一根线",
        category="工具",
        aliases=("靓机助手", "鲁大师", "验机精灵", "验机侠", "爬虫", "工具读出", "用户判断"),
        meaning="手机、平板电脑和笔记本场景中的验机工具或工具读数",
        usage="通常指验机工具或工具读数，不代表屏幕上真的出现了一根线。",
    ),
    TerminologyEntry(
        term="DLC版",
        category="商品版本",
        aliases=("扩充票", "Expansion Pass", "追加内容"),
        meaning="实体游戏卡自带的追加内容版本",
        usage="仅包装盒或卡带明确标有“+扩充票/Expansion Pass”时按DLC版理解；普通卡带后续在商城购买的追加内容仍按普通版。",
    ),
    TerminologyEntry(
        term="金手指",
        category="游戏卡带部件",
        aliases=("卡带触点", "金属触点"),
        meaning="游戏卡带与主机接触读取的金属触点",
        usage="该侧编码通常是游戏编码及生产批次，不是设备唯一序列号。",
    ),
    TerminologyEntry(
        term="箱说齐全",
        category="包装状态",
        aliases=("箱说", "包装说明齐全"),
        meaning="包装盒和说明资料齐全",
        usage="这是游戏卡带包装查验词，不代表卡带功能、语言或版本正常。",
    ),
    TerminologyEntry(
        term="笔尖/笔芯/笔帽",
        category="手写笔部件",
        aliases=("笔头", "替换笔尖", "笔帽钢圈"),
        meaning="三个不同的手写笔部件",
        usage="笔尖是可拆换的外部书写头，笔芯是取下笔尖后可见的内部结构，笔帽是笔尾或充电口盖；部件不同不得合并为同一故障。",
    ),
    TerminologyEntry(
        term="磁吸配对",
        category="连接方式",
        aliases=("磁吸连接", "吸附配对"),
        meaning="手写笔吸附到匹配平板电脑后建立连接",
        usage="适用于支持磁吸配对的型号；无电量显示、显示“--%”或无法建立连接需与普通磁吸外观现象区分。",
    ),
    TerminologyEntry(
        term="压感",
        category="手写笔功能",
        aliases=("压力感应", "书写压感"),
        meaning="书写力度变化对应线条粗细变化",
        usage="压感失灵属于书写功能问题，不等同于无法配对、断连或触控断点。",
    ),
    TerminologyEntry(
        term="双击切换",
        category="手写笔功能",
        aliases=("轻点两下", "双击笔身"),
        meaning="通过双击手写笔指定位置切换书写工具",
        usage="仅支持对应功能且已在系统中开启时检测，不要与按键失灵或压感问题合并。",
    ),
    TerminologyEntry(
        term="查找定位",
        category="账号与定位",
        aliases=("定位功能", "查找App", "绑定定位账号"),
        meaning="手写笔与原账号绑定的查找或定位功能",
        usage="无法解绑属于账号绑定或回收限制问题，不是普通蓝牙配对失败。",
    ),
    TerminologyEntry(
        term="扩展内存",
        category="设备参数",
        aliases=("内存扩展", "虚拟内存"),
        meaning="借用存储空间形成的虚拟内存，不计入设备实际运行内存",
        usage="学习机容量核验时应以物理运行内存为准，不能把扩展数值与出厂内存相加。",
    ),
    TerminologyEntry(
        term="特殊来源机",
        category="设备来源",
        aliases=(
            "监管机",
            "政企定制机",
            "改版机",
            "改装机",
            "工程机",
            "官翻机",
            "BS机",
            "展示机",
        ),
        meaning="非普通零售来源、受管理或经过版本改动的设备",
        usage="这是设备来源或版本属性，不等同于普通账号绑定、系统升级或外观拆修。",
    ),
    TerminologyEntry(
        term="断触/乱触/漂移",
        category="触控异常",
        aliases=("触控断点", "鬼触", "触摸偏移"),
        meaning="三种不同的触控异常",
        usage="断触是连续滑动轨迹中断，乱触是未操作时自动触发，漂移是点击位置与响应位置不一致。",
    ),
    TerminologyEntry(
        term="还原与解绑",
        category="账号与系统操作",
        aliases=("恢复出厂设置", "账号解绑", "清除账号"),
        meaning="恢复出厂设置不一定解除账号绑定",
        usage="学习机不同品牌的还原与解绑路径不同，必须分别判断，不能因已还原就默认账号已解除。",
    ),
    TerminologyEntry(
        term="间歇性黑屏",
        category="屏幕显示现象",
        aliases=("闪屏", "屏幕无法显示"),
        meaning="学习机屏幕短暂黑屏后恢复的显示异常",
        usage="仅学习机标准中，黑屏后5秒内恢复按闪屏理解，持续超过5秒按屏幕无法显示理解。",
    ),
    TerminologyEntry(
        term="部分检机况",
        category="检测状态",
        aliases=("部分检测", "能检则检"),
        meaning="设备状态导致部分功能类或显示类项目无法检测的机况",
        usage="无法检测的功能或显示项可勾选不检测，其他可检测项目仍需正常检查；不等同于整机不回收。",
    ),
    TerminologyEntry(
        term="快门数",
        category="检测指标",
        aliases=("快门次数", "快门寿命计数"),
        meaning="相机累计执行快门动作的次数",
        usage="这是相机使用程度属性，不是快门按键功能，也不是拍摄文件数量。",
    ),
    TerminologyEntry(
        term="A/Av、S/Tv、P、M、B门",
        category="相机功能",
        aliases=("光圈优先", "快门优先", "程序自动", "手动曝光", "慢门"),
        meaning="相机曝光模式",
        usage="A/Av为光圈优先，S/Tv为快门优先，P为程序自动，M为手动曝光，B门为长时间曝光；不要当作型号或报错代码。",
    ),
    TerminologyEntry(
        term="AF/MF",
        category="对焦方式",
        aliases=("自动对焦", "手动对焦", "对焦模式"),
        meaning="自动对焦与手动对焦模式",
        usage="MF手动镜头出现无电子光圈信息可能是正常现象，不应直接判为镜头未连接。",
    ),
    TerminologyEntry(
        term="EXIF",
        category="检测信息",
        aliases=("照片属性", "照片解析", "拍摄元数据"),
        meaning="照片文件中的拍摄元数据",
        usage="可用于读取机型、序列号或快门信息；它是信息查询来源，不是照片画面内容。",
    ),
    TerminologyEntry(
        term="热靴",
        category="相机机身部件",
        aliases=("热靴接口", "闪光灯接口"),
        meaning="相机顶部连接闪光灯或附件的接口",
        usage="热靴损坏属于机身接口或附件连接问题，不是镜头卡口或数据接口问题。",
    ),
    TerminologyEntry(
        term="屈光度",
        category="相机功能",
        aliases=("屈光度旋钮", "取景器清晰度调节"),
        meaning="按使用者视力调节取景器清晰度的功能",
        usage="调节屈光度只改变取景器观看清晰度，不改变镜头实际对焦或拍摄成像。",
    ),
    TerminologyEntry(
        term="取景器",
        category="相机机身部件",
        aliases=("EVF", "电子取景器", "目镜"),
        meaning="相机用于观察拍摄画面的独立取景部件",
        usage="取景器显示异常、取景器外观和后置主屏问题应分别聚类。",
    ),
    TerminologyEntry(
        term="CMOS/CCD",
        category="相机机身部件",
        aliases=("感光元件", "图像传感器", "低通滤镜"),
        meaning="相机机身内部负责形成拍摄图像的传感器组件",
        usage="传感器或低通滤镜问题属于成像或机身内部问题，不是屏幕显示问题。",
    ),
    TerminologyEntry(
        term="成像有斑",
        category="成像现象",
        aliases=("照片有斑", "机身有斑", "镜头有斑"),
        meaning="瑕疵出现在拍摄照片中",
        usage="拖动照片时斑点随画面移动属于成像问题，固定在屏幕同一位置属于显示问题；光圈或变焦变化可辅助区分机身与镜头原因。",
    ),
    TerminologyEntry(
        term="镜头报错与机身报错",
        category="故障现象",
        aliases=("Err", "E错误", "触点报错"),
        meaning="相机错误代码可能由镜头或机身引起",
        usage="更换镜头后仍重复报错才优先按机身报错；更换后消失则优先按镜头或触点问题。",
    ),
    TerminologyEntry(
        term="卡口",
        category="相机接口",
        aliases=("镜头卡口", "EF卡口", "RF卡口", "Z卡口", "E卡口", "M4/3卡口"),
        meaning="镜头与相机机身连接的接口规格",
        usage="卡口类型是兼容属性，卡口变形、错位或无法锁死才属于功能或外观异常。",
    ),
    TerminologyEntry(
        term="镜头操作环",
        category="相机镜头部件",
        aliases=("变焦环", "对焦环", "光圈环"),
        meaning="镜头上分别控制焦段、焦点和光圈的旋转部件",
        usage="三个操作环控制目标不同，卡顿、失灵或空转时不要合并为同一部件问题。",
    ),
    TerminologyEntry(
        term="小痰盂",
        category="镜头俗称",
        aliases=("50mm定焦", "廉价50定"),
        meaning="低价50mm定焦镜头的俗称",
        usage="部分此类镜头采用普通对焦马达，对焦时出现滋滋声可能是正常现象，不能仅凭声音判异响。",
    ),
    TerminologyEntry(
        term="蒙皮/胶皮",
        category="外观部件",
        aliases=("镜头蒙皮", "机身蒙皮", "橡胶皮"),
        meaning="机身或镜头外部的橡胶覆盖层",
        usage="脱胶、破损、老化和鼓包是不同现象；不要与金属或塑料外壳碎裂混为一类。",
    ),
    TerminologyEntry(
        term="油光",
        category="使用磨损现象",
        aliases=("发亮", "反光发油"),
        meaning="哑光握持区域因长期摩擦和皮脂附着形成的发亮现象",
        usage="油光属于使用磨损外观，不等同于液体污渍、浸液或补漆。",
    ),
    TerminologyEntry(
        term="镜片进灰/异物/发霉",
        category="镜头内部现象",
        aliases=("镜头进灰", "镜内异物", "镜片霉菌"),
        meaning="三类不同的镜头内部现象",
        usage="进灰是无生长结构的细小颗粒或纤维；异物是金属屑、塑料屑、绒毛团或虫尸等外来物；发霉具有丝状、网状、放射状或树枝状生长结构。",
    ),
    TerminologyEntry(
        term="镀膜与消光漆",
        category="镜头涂层",
        aliases=("镜片镀膜", "光学镀膜", "消光涂层", "内部掉漆"),
        meaning="两个不同位置和用途的涂层",
        usage="镀膜位于光学镜片表面；消光漆位于镜头内部非镜片表面，用于减少反光，二者脱落不得合并。",
    ),
    TerminologyEntry(
        term="UV镜与脚架环",
        category="相机配件",
        aliases=("保护镜", "滤镜", "镜头脚架环", "三脚架环"),
        meaning="镜头的可拆卸外部配件",
        usage="UV镜或镜框无法取下属于配件操作问题；脚架环用于支撑大镜头，不是对焦环或变焦环。",
    ),
    TerminologyEntry(
        term="光圈显示F--/F00/00/0",
        category="镜头连接状态",
        aliases=("F--", "F00", "光圈00", "光圈0"),
        meaning="机身未电子识别到镜头",
        usage="自动镜头出现时通常检查连接、触点或卡口；纯手动对焦镜头可能正常显示该状态。",
    ),
    TerminologyEntry(
        term="改版机与补漆",
        category="拆修现象",
        aliases=("改版镜头", "型号改写", "明显补漆", "轻微补漆", "喷漆"),
        meaning="型号信息被改动或外壳经过后期重新上漆",
        usage="改版关注实物型号与系统或照片读取型号是否一致；补漆关注掉色、漆渣、色差、颗粒或覆盖原标识，二者不是普通掉漆。",
    ),
    TerminologyEntry(
        term="A/B/C/D面",
        category="机身部件",
        aliases=("A面", "B面", "C面", "D面", "AB面", "CD面"),
        meaning="笔记本外壳和屏幕侧的四个位置名称",
        usage="A面通常为屏幕后盖，B面为屏幕和边框，C面为键盘、掌托和触控板区域，D面为底壳；各面的外观和拆修问题不得混为同一部件。",
    ),
    TerminologyEntry(
        term="BIOS/D壳序列号",
        category="设备信息",
        aliases=("BIOS序列号", "D壳序列号", "后壳序列号", "质检码"),
        meaning="笔记本序列号的不同读取或标注位置",
        usage="Windows通常优先读取BIOS序列号，Mac可读取关于本机序列号；D壳或后壳序列号是外壳标签信息，不能在来源不一致时直接视为同一个值。",
    ),
    TerminologyEntry(
        term="集显/独显",
        category="硬件类型",
        aliases=("集成显卡", "核心显卡", "核显", "独立显卡"),
        meaning="集成在处理器中的显卡与独立配置的显卡",
        usage="集显型号常带Graphics字样；独显通常以GeForce、Radeon等具体系列标识，二者是硬件配置类型，不是显卡故障现象。",
    ),
    TerminologyEntry(
        term="设备锁",
        category="账号与管理限制",
        aliases=("ID账号未解除", "管理员权限锁", "BIOS锁", "监管锁", "固件锁"),
        meaning="账号、固件、BIOS或管理权限形成的设备限制",
        usage="不同锁的解除路径和回收限制不同；系统能开机或完成还原，不代表这些限制已经解除。",
    ),
    TerminologyEntry(
        term="充电器INPUT/OUTPUT",
        category="配件参数",
        aliases=("INPUT", "OUTPUT", "输入电压", "输出电压", "输出电流", "3C认证"),
        meaning="充电器输入与输出参数标识",
        usage="INPUT表示充电器接收的电压和电流，OUTPUT表示向笔记本输出的电压和电流；参数核对属于配件检测，不是笔记本接口故障本身。",
    ),
    TerminologyEntry(
        term="CTO",
        category="设备配置",
        aliases=("定制配置", "按订单配置", "Custom To Order"),
        meaning="按订单定制的出厂配置",
        usage="Mac等设备显示CTO时，内存或存储容量可能不同于常规零售配置，不能仅因容量与普通型号不一致就判为后期扩容。",
    ),
    TerminologyEntry(
        term="偏光膜",
        category="屏幕部件",
        aliases=("偏振膜", "屏幕偏光膜"),
        meaning="屏幕内部或表面的光学膜层部件",
        usage="偏光膜破损、缺失属于屏幕部件损坏；它不是“偏光检测”工具或检测动作。",
    ),
    TerminologyEntry(
        term="漏液",
        category="屏幕显示现象",
        aliases=("液晶漏液", "屏幕漏液"),
        meaning="液晶层受损后出现黑块、彩块或扩散状显示异常",
        usage="漏液属于屏幕内部显示故障，不等同于机身进水或浸液痕迹。",
    ),
    TerminologyEntry(
        term="透图",
        category="屏幕显示现象",
        aliases=("残影", "图像残留", "屏幕透图"),
        meaning="屏幕残留先前图像或文字轮廓",
        usage="在其他图片、应用或纯色界面上仍能看到旧图像印迹时按透图理解；不要与屏幕表面的键盘印记合并。",
    ),
    TerminologyEntry(
        term="色斑/亮斑/亮点/坏点",
        category="屏幕显示现象",
        aliases=("色斑", "亮斑", "亮点", "坏点", "彩色坏点"),
        meaning="不同形态的屏幕显示异常",
        usage="色斑是在纯色画面出现异常色块，亮斑是在白底出现异常发亮区域，亮点或坏点是很小的异常像素点；三类现象应按实际表现分别判断。",
    ),
    TerminologyEntry(
        term="屏幕发黄/发红",
        category="屏幕显示现象",
        aliases=("白底发黄", "白底发红", "屏幕老化"),
        meaning="白色背景下屏幕边缘或区域出现黄色、红色老化色偏",
        usage="属于屏幕老化或色偏现象；能否被相机拍出是检测表现，不代表摄像头成像异常。",
    ),
    TerminologyEntry(
        term="漏光",
        category="屏幕显示现象",
        aliases=("屏幕漏光", "黑底漏光"),
        meaning="黑色背景下屏幕边缘出现不规则亮光",
        usage="漏光属于屏幕显示或装配现象；轻微均匀漏光可能属于正常范围，不等同于液晶漏液。",
    ),
    TerminologyEntry(
        term="屏幕碎裂/划痕",
        category="屏幕外观现象",
        aliases=("屏幕碎裂", "屏幕裂痕", "屏幕划痕", "屏幕缺角"),
        meaning="屏幕玻璃或表面受到物理损伤",
        usage="碎裂、裂痕、缺角和划痕属于屏幕外观损伤，不是亮斑、坏点、漏液或屏线等显示异常。",
    ),
    TerminologyEntry(
        term="屏幕气泡/磕点/印记",
        category="屏幕外观现象",
        aliases=("屏幕气泡", "磕点", "屏幕磕点", "印记", "键盘印记", "涂层脱落"),
        meaning="屏幕表面的三类外观现象",
        usage="气泡位于显示区域与面板之间，磕点是玻璃表面有凹陷且触摸有触感，印记是键盘按键痕或无法擦除的痕迹；不要与亮点、坏点或透图合并。",
    ),
    TerminologyEntry(
        term="屏幕拆修痕迹",
        category="拆修现象",
        aliases=(
            "屏幕脱胶",
            "脱胶缝隙",
            "胶条破损",
            "屏幕边缘溢胶",
            "屏幕撬痕",
            "屏幕第三方标识",
            "屏幕有第三方标识",
            "显示屏维修弹窗",
        ),
        meaning="屏幕或屏幕总成被拆卸、维修或更换的证据",
        usage="溢胶、撬痕、第三方维修标签、维修弹窗和异常胶条属于拆修证据，不应仅按普通屏幕外观损伤聚类。",
    ),
    TerminologyEntry(
        term="屏幕松动/屏内异物",
        category="屏幕部件",
        aliases=("屏幕松动", "屏内异物", "屏幕进灰"),
        meaning="屏幕装配松动或内部进入颗粒异物",
        usage="屏幕边框与显示面板错位、可合拢或内部存在明显颗粒时按屏幕装配或内部问题理解，不是显示坏点。",
    ),
    TerminologyEntry(
        term="原彩显示",
        category="屏幕功能",
        aliases=("原彩", "True Tone", "无原彩"),
        meaning="根据环境光调节屏幕色温的显示功能",
        usage="支持原彩的Mac机型缺失该功能可能与屏幕更换或配置有关；原彩不是分辨率、亮度或色域参数。",
    ),
    TerminologyEntry(
        term="指点杆",
        category="输入部件",
        aliases=("小红帽", "小蓝帽", "TrackPoint"),
        meaning="部分笔记本键盘中间用于移动光标的小型操控杆",
        usage="指点杆帽缺失属于输入部件外观问题，不能与触控板、鼠标或键帽缺失合并。",
    ),
    TerminologyEntry(
        term="键盘自动输入",
        category="输入功能异常",
        aliases=("自动输入", "连键", "鬼键", "键盘乱按"),
        meaning="未正常按键时键盘自行输入或一次按键产生多次字符",
        usage="自动输入属于键盘功能异常；按键缺失、油光或布局不符属于不同的键盘外观或配置问题。",
    ),
    TerminologyEntry(
        term="摄像头镜片与物理开关",
        category="摄像头部件",
        aliases=(
            "摄像头镜片",
            "摄像头灯罩",
            "摄像头物理快捷键",
            "摄像头开关",
            "摄像头镜片碎裂",
        ),
        meaning="笔记本摄像头相关的外部部件",
        usage="镜片或灯罩碎裂属于摄像头部件外观问题，物理快捷键或开关失灵属于摄像头控制部件问题，不等同于摄像头画面异常。",
    ),
    TerminologyEntry(
        term="摄像头泛红/泛蓝/模糊",
        category="摄像头成像现象",
        aliases=("摄像头泛红", "摄像头泛蓝", "摄像头模糊", "画面雪花", "画面颗粒"),
        meaning="摄像头开启后画面出现偏色或清晰度异常",
        usage="这是摄像头成像问题，不是笔记本屏幕发红、发黄、花屏或屏幕亮斑。",
    ),
    TerminologyEntry(
        term="主板拆修痕迹",
        category="拆修现象",
        aliases=("飞线", "锡焊", "焊锡", "助焊剂", "烧伤痕迹", "第三方维修标识", "维修章"),
        meaning="主板或内部零件被焊接、飞线或维修的证据",
        usage="残留焊锡、飞线、助焊剂、异常焊点、烧伤和第三方维修标识属于内部拆修证据，不是普通灰尘或散热硅脂。",
    ),
    TerminologyEntry(
        term="板载扩容",
        category="拆修现象",
        aliases=("板载内存扩容", "板载硬盘扩容", "硬盘扩容", "内存扩容"),
        meaning="直接改动主板焊接存储或内存容量",
        usage="板载扩容属于主板级改装；它不同于插槽内正常加装或更换可拆卸硬盘、内存。",
    ),
    TerminologyEntry(
        term="加装/更换硬件",
        category="拆修现象",
        aliases=(
            "加装硬盘",
            "更换硬盘",
            "加装内存",
            "更换内存",
            "更换第三方硬盘",
            "更换第三方内存",
        ),
        meaning="插槽内硬盘或内存经过后期增加或替换",
        usage="加装或更换可拆卸硬件属于配置变化或拆修记录，不等同于板载扩容，也不能仅凭品牌不同判断功能故障。",
    ),
    TerminologyEntry(
        term="螺丝拆修痕迹",
        category="拆修现象",
        aliases=("螺丝断裂", "内部螺丝缺失", "螺丝滑丝", "螺丝凸起", "螺丝更换", "机身螺丝缺少"),
        meaning="螺丝缺失、损坏或被更换形成的拆装证据",
        usage="滑丝是螺纹或螺丝槽损坏导致无法锁紧，凸起是螺丝未正常落位；应与正常可拆卸盖板的螺丝缺失规则区分。",
    ),
    TerminologyEntry(
        term="电池鼓包",
        category="电池现象",
        aliases=("电池气体回弹", "电池膨胀", "鼓包电池"),
        meaning="电池内部产气造成外壳鼓起",
        usage="拆机按压电池表面有明显气体回弹或电池推动外壳变形时按鼓包理解；不要与电池健康度低或续航下降合并。",
    ),
    TerminologyEntry(
        term="浸液痕迹",
        category="浸液现象",
        aliases=(
            "浸液",
            "进液",
            "水渍",
            "霉斑",
            "金属生锈",
            "水渍光斑",
            "防水标签变红",
            "螺丝生锈",
            "机身外观生锈",
            "进水痕迹",
        ),
        meaning="液体进入机身后留下的水渍、锈蚀或霉变证据",
        usage="主板水渍、霉斑、金属锈蚀、防水标签变红和灯带进水痕迹属于浸液证据；水渍光斑不要与屏幕亮斑合并，浸液也不同于液晶漏液。",
    ),
    TerminologyEntry(
        term="转轴/合盖息屏",
        category="结构部件与功能",
        aliases=("转轴", "合盖息屏", "转轴卡顿", "转轴异响"),
        meaning="笔记本屏幕转轴结构与合上屏幕后触发休眠的功能",
        usage="转轴松动、卡顿或异响属于机械结构问题；合盖不息屏需先确认系统电源设置，再判断合盖检测功能。",
    ),
    TerminologyEntry(
        term="性能跑分",
        category="检测方法",
        aliases=("跑分", "性能分", "视频帧率测试", "耗电测试"),
        meaning="通过验机工具执行的性能、帧率或耗电测试",
        usage="跑分或帧率是检测结果，不是硬件部件；低于同配置基准时才形成性能问题，不能仅凭“跑分”一词判故障。",
    ),
    TerminologyEntry(
        term="闪屏/花屏/屏线",
        category="屏幕显示现象",
        aliases=("闪屏", "花屏", "屏线", "液晶屏线条", "显示线条"),
        meaning="笔记本屏幕闪烁、画面错乱或出现异常线条",
        usage="属于屏幕显示问题；屏线在此表示画面线条异常，不是屏幕排线部件，也不要与验机工具中的“一根线”合并。",
    ),
)


SUPPORTED_TERMINOLOGY_CATEGORIES = frozenset(
    {
        "使用磨损现象",
        "包装状态",
        "商品版本",
        "外观部件",
        "对焦方式",
        "屏幕功能",
        "屏幕外观现象",
        "屏幕显示现象",
        "屏幕部件",
        "工具",
        "成像现象",
        "手写笔功能",
        "手写笔部件",
        "拆修现象",
        "摄像头成像现象",
        "摄像头部件",
        "故障现象",
        "机身部件",
        "检测信息",
        "检测指标",
        "检测方法",
        "检测状态",
        "浸液现象",
        "游戏卡带部件",
        "电池现象",
        "相机功能",
        "相机接口",
        "相机机身部件",
        "相机配件",
        "相机镜头部件",
        "硬件类型",
        "结构部件与功能",
        "触控异常",
        "设备信息",
        "设备参数",
        "设备来源",
        "设备配置",
        "账号与定位",
        "账号与管理限制",
        "账号与系统操作",
        "输入功能异常",
        "输入部件",
        "连接方式",
        "配件参数",
        "镜头俗称",
        "镜头内部现象",
        "镜头涂层",
        "镜头连接状态",
    }
)

_DISPLAY_PRODUCTS = ("手机", "平板电脑", "笔记本", "学习机")
_CAMERA_BODY_PRODUCTS = ("单电/微单机身", "单反机身")
_PRODUCT_SCOPE_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("手机", "平板电脑"), ("偏光检测", "红线检测")),
    (("手机", "平板电脑", "笔记本"), ("一根线", "原彩显示")),
    (("游戏机", "游戏卡带"), ("DLC版", "金手指", "箱说齐全")),
    (
        ("手写笔",),
        ("笔尖/笔芯/笔帽", "磁吸配对", "压感", "双击切换", "查找定位"),
    ),
    (
        ("学习机",),
        ("扩展内存", "还原与解绑", "间歇性黑屏", "部分检机况"),
    ),
    (("学习机", "笔记本"), ("特殊来源机", "断触/乱触/漂移")),
    (
        _CAMERA_BODY_PRODUCTS,
        (
            "快门数",
            "A/Av、S/Tv、P、M、B门",
            "EXIF",
            "热靴",
            "屈光度",
            "取景器",
            "CMOS/CCD",
            "成像有斑",
            "镜头报错与机身报错",
        ),
    ),
    (
        (*_CAMERA_BODY_PRODUCTS, "相机镜头"),
        ("AF/MF", "卡口", "蒙皮/胶皮", "油光", "改版机与补漆"),
    ),
    (
        ("相机镜头",),
        (
            "镜头操作环",
            "小痰盂",
            "镜片进灰/异物/发霉",
            "镀膜与消光漆",
            "UV镜与脚架环",
            "光圈显示F--/F00/00/0",
        ),
    ),
    (
        _DISPLAY_PRODUCTS,
        (
            "偏光膜",
            "漏液",
            "透图",
            "色斑/亮斑/亮点/坏点",
            "屏幕发黄/发红",
            "漏光",
            "屏幕碎裂/划痕",
            "浸液痕迹",
        ),
    ),
    (
        ("笔记本",),
        (
            "A/B/C/D面",
            "BIOS/D壳序列号",
            "集显/独显",
            "设备锁",
            "充电器INPUT/OUTPUT",
            "CTO",
            "屏幕气泡/磕点/印记",
            "屏幕拆修痕迹",
            "屏幕松动/屏内异物",
            "指点杆",
            "键盘自动输入",
            "摄像头镜片与物理开关",
            "摄像头泛红/泛蓝/模糊",
            "主板拆修痕迹",
            "板载扩容",
            "加装/更换硬件",
            "螺丝拆修痕迹",
            "电池鼓包",
            "转轴/合盖息屏",
            "性能跑分",
            "闪屏/花屏/屏线",
        ),
    ),
)


def _build_scoped_entries() -> tuple[TerminologyEntry, ...]:
    scopes_by_term: dict[str, tuple[str, ...]] = {}
    for product_categories, terms in _PRODUCT_SCOPE_GROUPS:
        for term in terms:
            if term in scopes_by_term:
                raise ValueError(f"术语重复配置适用品类：{term}")
            scopes_by_term[term] = product_categories

    raw_terms = {entry.term for entry in _RAW_TERMINOLOGY_ENTRIES}
    configured_terms = set(scopes_by_term)
    if raw_terms != configured_terms:
        missing = sorted(raw_terms - configured_terms)
        extra = sorted(configured_terms - raw_terms)
        raise ValueError(f"术语适用品类配置不完整：missing={missing}, extra={extra}")

    entries = tuple(
        replace(entry, product_categories=scopes_by_term[entry.term])
        for entry in _RAW_TERMINOLOGY_ENTRIES
    )
    unsupported_categories = {
        entry.category for entry in entries
    } - SUPPORTED_TERMINOLOGY_CATEGORIES
    if unsupported_categories:
        raise ValueError(f"存在未注册术语分类：{sorted(unsupported_categories)}")
    return entries


TERMINOLOGY_ENTRIES = _build_scoped_entries()


def _normalize_terminology_token(value: str) -> str:
    return value.strip().casefold()


def _normalize_product_categories(
    product_categories: str | Iterable[str] | None,
) -> tuple[str, ...]:
    if product_categories is None:
        return ()
    values = (product_categories,) if isinstance(product_categories, str) else product_categories
    return tuple(
        dict.fromkeys(
            canonical_product_name(value, unknown="") or value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        )
    )


def _entry_applies_to(
    entry: TerminologyEntry,
    product_categories: tuple[str, ...],
) -> bool:
    return not product_categories or bool(
        set(entry.product_categories).intersection(product_categories)
    )


def _build_terminology_lookup() -> dict[str, tuple[TerminologyEntry, ...]]:
    lookup_lists: dict[str, list[TerminologyEntry]] = {}
    for entry in TERMINOLOGY_ENTRIES:
        for token in (entry.term, *entry.aliases):
            key = _normalize_terminology_token(token)
            entries = lookup_lists.setdefault(key, [])
            if entry not in entries:
                entries.append(entry)
    return {key: tuple(entries) for key, entries in lookup_lists.items()}


TERMINOLOGY_LOOKUP = _build_terminology_lookup()
TERMINOLOGY_CATEGORIES = frozenset(entry.category for entry in TERMINOLOGY_ENTRIES)


def _terminology_fingerprint() -> str:
    payload = "\n".join(
        "|".join(
            (
                entry.term,
                entry.category,
                ",".join(entry.aliases),
                entry.meaning,
                entry.usage,
                ",".join(entry.product_categories),
            )
        )
        for entry in TERMINOLOGY_ENTRIES
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def terminology_metadata() -> dict[str, object]:
    """Return the machine-readable identity of the shared terminology dictionary."""
    entries = tuple(TERMINOLOGY_ENTRIES)
    return {
        "loaded": bool(entries),
        "source": "embedded",
        "version": f"terminology-{_terminology_fingerprint()}",
        "entry_count": len(entries),
        "categories": sorted({entry.category for entry in entries}),
        "product_categories": sorted(
            {
                product_category
                for entry in entries
                for product_category in entry.product_categories
            }
        ),
    }


def ensure_terminology_loaded() -> dict[str, object]:
    """Fail fast instead of allowing a run to continue without shared terminology."""
    metadata = terminology_metadata()
    if not metadata["loaded"] or not metadata["entry_count"]:
        raise TerminologyError("术语字典未加载：禁止继续运行。")
    invalid_entries = [
        entry.term
        for entry in TERMINOLOGY_ENTRIES
        if not (
            entry.term.strip()
            and entry.category.strip()
            and entry.meaning.strip()
            and entry.usage.strip()
            and entry.product_categories
        )
    ]
    if invalid_entries:
        raise TerminologyError(
            f"术语字典存在无效条目：{', '.join(invalid_entries[:5])}"
        )
    return metadata


def lookup_terminology(
    value: str,
    product_category: str | None = None,
) -> TerminologyEntry | None:
    candidates = TERMINOLOGY_LOOKUP.get(_normalize_terminology_token(value), ())
    requested_categories = _normalize_product_categories(product_category)
    matches = tuple(
        entry
        for entry in candidates
        if _entry_applies_to(entry, requested_categories)
    )
    return matches[0] if len(matches) == 1 else None


def find_terminology_entries(
    text: str,
    product_category: str | None = None,
) -> tuple[TerminologyEntry, ...]:
    normalized_text = _normalize_terminology_token(text)
    requested_categories = _normalize_product_categories(product_category)
    candidates: list[tuple[int, int, TerminologyEntry]] = []
    for token, entries in TERMINOLOGY_LOOKUP.items():
        start = normalized_text.find(token)
        while start >= 0:
            end = start + len(token)
            for entry in entries:
                if _entry_applies_to(entry, requested_categories):
                    candidates.append((start, end, entry))
            start = normalized_text.find(token, start + 1)

    selected: list[tuple[int, int, TerminologyEntry]] = []
    for start, end, entry in sorted(
        candidates,
        key=lambda item: (item[0], -(item[1] - item[0]), item[2].term),
    ):
        if any(start < selected_end and end > selected_start for selected_start, selected_end, _ in selected):
            continue
        selected.append((start, end, entry))
    unique_entries: list[TerminologyEntry] = []
    seen_entries: set[TerminologyEntry] = set()
    for _, _, entry in selected:
        if entry in seen_entries:
            continue
        seen_entries.add(entry)
        unique_entries.append(entry)
    return tuple(unique_entries)


def build_terminology_prompt_block(
    product_categories: str | Iterable[str] | None = None,
) -> str:
    metadata = ensure_terminology_loaded()
    requested_categories = _normalize_product_categories(product_categories)
    lines = [
        "=== 术语字典（全流程共享）===",
        f"字典状态：已加载；版本:{metadata['version']}；条目:{metadata['entry_count']}",
    ]
    matching_entries = tuple(
        entry
        for entry in TERMINOLOGY_ENTRIES
        if _entry_applies_to(entry, requested_categories)
    )
    if not matching_entries and requested_categories:
        lines.append(
            "当前输入品类未匹配到专属术语条目；字典已加载，但本次必须进入人工确认。"
        )
    for index, entry in enumerate(matching_entries, start=1):
        aliases = " / ".join((entry.term, *entry.aliases))
        scopes = "/".join(entry.product_categories)
        lines.append(
            f"{index}. [{entry.category}] {aliases}（适用:{scopes}）："
            f"{entry.meaning}。{entry.usage}"
        )
    return "\n".join(lines)
