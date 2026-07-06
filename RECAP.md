# 答疑中台 - 知识库管理模块 Recap

## 项目概述

曼哈顿答疑部门中台项目的**方向三：知识运营模块**，负责知识库的创建、编辑、审核、发布全流程。

### 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端 | Python 3.14 + FastAPI + SQLAlchemy | RESTful API，端口 8001 |
| 数据库 | PostgreSQL 15（Docker） | 端口 5432 |
| 搜索引擎 | Elasticsearch 8（Docker） | 端口 9200，预留全文检索 |
| 缓存 | Redis 7（Docker） | 端口 6379，预留会话缓存 |
| 前端 | Vue 3 (CDN) + 原生 CSS | 单页 HTML，无需构建工具 |

### 启动方式

```powershell
# 后端
Set-Location "C:\Users\a1873\Documents\答疑中台知识库项目\backend"
Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8001" -WindowStyle Hidden

# 前端访问
# http://localhost:8001/app
# API 文档: http://localhost:8001/docs
```

### 端口说明

- **8001**：后端 API + 前端静态服务 + 文件上传访问
- **8000**：被其他进程占用（管理员权限），暂时不用

---

## 已完成功能

### 知识条目 CRUD

- 新建知识（标题、层级 L1/L2/L3、分类、创建人、适用场景、富文本内容）
- 编辑知识（一体化编辑页面，模块化内容块）
- 删除知识、废弃知识
- 按状态/层级/关键词筛选列表
- 统计卡片（全部/已发布/待审核/草稿）

### 富文本内容编辑器

- **一级模块**：文本块，支持多行文本
- **二级模块**：文本块内可插入图片/视频子模块
- 文本块可独立添加、删除
- 图片/视频子模块可独立添加、删除
- 空文本块不冗余存储（保存时自动过滤）

### 媒体上传

- 点击选择文件上传（图片/视频）
- Ctrl+V 粘贴上传（自动识别图片/视频类型）
- 悬停在指定模块区域粘贴，直接上传到该模块
- 未悬停时粘贴，在末尾新建模块并上传
- 上传即时生效：监听到粘贴后直接上传到后端，不需要手动保存
- 上传成功后显示"已关联"，缩略图实时展示
- 未保存的媒体取消编辑时自动清理

### 缩略图与预览

- 图片缩略图（72x54px）+ hover 半透明遮罩显示"预览"
- 视频缩略图（96x54px）+ hover 半透明遮罩显示播放图标
- 点击缩略图弹出全屏预览层
- 图片：全屏查看原图，点击任意位置关闭
- 视频：全屏自动播放 + 播放控件，点击黑色遮罩关闭，点击视频本身不关闭
- 视频缩略图禁用所有浏览器原生控件（画中画、下载、全屏按钮）

### 审核流程

- 草稿 → 提交审核 → 审批通过 → 已发布
- 草稿 → 废弃
- 状态变更实时刷新列表

### 数据同步

前端编辑器中修改的图片标题/说明（alt/caption），保存时同步写入：
1. `content.blocks` JSON（内容块级别）
2. `knowledge_media` 表（媒体记录级别）

重新编辑时优先从 `content.blocks` 读取，兜底从 `media` 表读取。

---

## 后端 API 接口

### 知识条目

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/knowledge` | 创建知识条目 |
| GET | `/api/v1/knowledge` | 查询列表（支持 status/layer/keyword/page/size） |
| GET | `/api/v1/knowledge/{id}` | 获取详情 |
| PATCH | `/api/v1/knowledge/{id}` | 更新条目 |
| DELETE | `/api/v1/knowledge/{id}` | 删除条目 |
| POST | `/api/v1/knowledge/{id}/submit-review` | 提交审核 |
| POST | `/api/v1/knowledge/{id}/approve` | 审批通过 |
| POST | `/api/v1/knowledge/{id}/deprecate` | 废弃 |

### 媒体文件

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/knowledge/{id}/media` | 上传媒体（multipart form） |
| GET | `/api/v1/knowledge/{id}/media` | 获取媒体列表 |
| PATCH | `/api/v1/knowledge/{id}/media/{filename}` | 更新媒体信息 |
| DELETE | `/api/v1/knowledge/{id}/media/{filename}` | 删除媒体文件 |
| POST | `/api/v1/knowledge/upload-temp` | 临时上传（无需知识ID） |

### 文件访问

| 路径 | 说明 |
|------|------|
| `/uploads/{filename}` | 访问已上传的媒体文件 |
| `/lib/{filename}` | 前端依赖库（Vue/Element Plus） |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/knowledge/search` | 检索知识库 |
| POST | `/api/v1/knowledge/candidates` | 提交候选知识 |
| POST | `/api/v1/knowledge/feedback` | 使用反馈 |
| GET | `/api/v1/categories` | 分类列表 |
| GET | `/api/v1/tags` | 标签维度列表 |
| GET | `/health` | 健康检查 |

---

## 项目结构

```
答疑中台知识库项目/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口，路由挂载，静态文件
│   │   ├── core/
│   │   │   ├── config.py        # 配置（数据库URL、版本号等）
│   │   │   └── database.py      # SQLAlchemy 引擎和 Session
│   │   ├── models/
│   │   │   └── knowledge.py     # 数据模型（Knowledge, KnowledgeMedia, Category, Tag等）
│   │   ├── schemas/
│   │   │   └── knowledge.py     # Pydantic 请求/响应 schema
│   │   └── routes/
│   │       ├── knowledge.py     # 知识/媒体 CRUD + 检索 + 反馈
│   │       ├── category.py      # 分类管理
│   │       └── tag.py           # 标签管理
│   ├── uploads/                 # 上传文件存储（不进版本控制）
│   ├── venv/                    # Python 虚拟环境（不进版本控制）
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── index.html               # 主界面（知识列表 + 编辑弹窗）
│   └── lib/                     # 前端依赖（Vue3, Element Plus）
├── scripts/
│   ├── start.ps1                # 启动脚本
│   └── start-sandbox.ps1        # 沙箱启动脚本
├── docs/                        # 设计文档
├── docker-compose.yml           # PG/ES/Redis 容器编排
├── .gitignore
└── RECAP.md                     # 本文件
```

---

## 已知约束 & 后续计划

### 当前约束

- 端口 8000 被占用（管理员权限进程），暂用 8001
- `pip install` 需要提权访问外部网络
- 前端不依赖 npm，直接 CDN 加载 Vue 3

### 后续计划

1. **分类/标签体系**：分类管理页面 + 标签维度管理页面
2. **全文检索**：对接 Elasticsearch，实现知识内容全文搜索
3. **与其他方向衔接**：
   - 方向二（自动标注）：通过 API 自动提交候选知识
   - 方向四（检索推荐）：通过 `/search` 接口提供检索能力
4. **可视化启停控制面板**：项目完结时做一个服务器启动/关闭的可视化操作界面

---

## 关键文件变更日志

### 2026-07-07

- **前端 index.html**：完整重写一体化编辑页面
  - 模块化内容编辑器（文本块 + 图片/视频子模块）
  - 粘贴上传（Ctrl+V，支持图片和视频）
  - 缩略图 + hover 遮罩 + 全屏预览（图片/视频）
  - 保存时过滤空文本块
  - 编辑时从 content.blocks + media 表双源重建子模块
  - 关闭编辑不再误删已保存媒体
  - 视频缩略图禁用浏览器原生控件

- **后端 knowledge.py**：
  - `_sync_media_meta()`：保存时同步 blocks 里的 alt/caption 回 media 表
  - `delete_media` / `update_media`：改用 filename 查找（和前端传值一致）
  - `upload_media`：补齐 original_name / alt / caption 默认值
  - `update_knowledge`：content 更新时触发 media 表同步

- **.gitignore**：排除 uploads/、临时测试页面、venv
