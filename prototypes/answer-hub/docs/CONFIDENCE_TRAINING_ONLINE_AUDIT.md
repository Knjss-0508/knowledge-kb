# 线上置信度训练闭环审计（2026-09-29）

## 结论

置信度训练功能当前属于 CZ 服务器侧功能，不是组内电脑 Answer Hub Worker 的功能。本交付包没有漏传组内电脑所需的 Answer Hub 工作流；同时刻意不把 CZ 后端、前端、PostgreSQL 或数据库迁移复制到组内电脑。

## 已核对的线上证据

- 后端容器：knowledge-kb-backend:blind-label-ui-20260929
- OpenAPI 已暴露 12 个 /api/v1/confidence-training 接口
- 已注册前端入口和操作：概览、人工真值、模型连接测试、训练候选任务、影子复跑、退化复核、Prompt 修订、导出
- 已注册后台影子复跑 Worker，应用启动时会恢复未完成的影子任务
- 已存在并执行的迁移链：
  20260928_01 -> 20260928_02 -> 20260928_03 -> 20260928_04 -> 20260928_05
- 数据库表已存在：
  confidence_training_settings
  confidence_training_jobs
- 当前两张表记录数均为 0，尚未用实际数据创建设置或训练任务

## 闭环判断

已具备的代码闭环：
1. 候选复核数据读取
2. 人工真值修订
3. DeepSeek-flash 标注/纠错
4. 训练候选统计与导出
5. 训练任务创建
6. 影子 Prompt 复跑
7. 退化样本与回归复核
8. 候选 Prompt 修订
9. 结果写回 CZ 数据库

尚未证明的运行闭环：
1. 尚无实际置信度训练任务记录
2. 尚无一次真实的影子复跑完成记录
3. 尚无一次真实 Prompt 修订完成记录
4. 没有专门的 test_confidence_training.py；当前自动化测试主要覆盖候选复核和前端
5. DeepSeek-flash 真实连接、样本标注、退化回归和导出结果仍需用脱敏样本验收

## 组内电脑部署边界

组内电脑只部署：
- Answer Hub API
- Answer Hub Queue Worker
- 本地任务队列、运行结果和日志
- 组内模型配置

置信度训练的 CZ 侧代码仍部署在服务器：
- backend/app/routes/confidence_training.py
- backend/app/services/confidence_training.py
- backend/app/services/model_annotation.py
- backend/app/schemas/confidence_training.py
- backend/migrations/versions/20260928_01_add_confidence_training_settings.py
- backend/migrations/versions/20260928_02_add_confidence_training_jobs.py
- backend/migrations/versions/20260928_03_add_confidence_prompt_analysis.py
- backend/migrations/versions/20260928_04_add_confidence_shadow_evaluation.py
- backend/migrations/versions/20260928_05_add_confidence_regression_reviews.py

不要将上述 CZ 文件复制到组内电脑，也不要在组内电脑单独创建这些 PostgreSQL 表。组内电脑的 Answer Hub 结果仍通过 CZ 后端接口进入候选复核与置信度训练链路。
