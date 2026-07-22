# 交接包文件说明

压缩包根目录包含以下内容：

- `src/`：第三部分 Python 工作流。
- `streamlit_app.py`：本地工作台。
- `tests/`：回归测试。
- `examples/`：脱敏演示数据和标准目录。
- `handoff/`：CPU/GPU Embedding Compose、启动辅助脚本和本次交接说明。
- `pyproject.toml`、`.env.example`、`README.md`：安装与配置所需文件。
- `cz-knowledge-kb/knowledge-kb-master/`：cz 知识库源码快照，仅供对接参考。

不包含：`.env`、`.venv`、`data/*.db`、`outputs/`、`output/`、真实 Excel、模型缓存、Docker 镜像和日志。
