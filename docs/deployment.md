# 自适应部署

部署脚本始终使用 `Qwen/Qwen3-Embedding-0.6B` 和 1024 维向量。它只在运行时选择 GPU 或 CPU，不会替换模型、改变向量维度或修改数据库数据。

```bash
./scripts/deploy.sh --runtime auto
```

Windows PowerShell：

```powershell
.\scripts\deploy.ps1 -Runtime auto
```

`auto` 会先用配置的 `TEI_GPU_IMAGE` 验证 Docker 是否能创建 GPU 容器。验证通过时使用 `docker-compose.embedding-gpu.yml`；否则使用 `docker-compose.embedding-cpu.yml`。CPU 配置使用项目内的 Qwen CPU 服务，保持 `/v1/embeddings` 协议兼容。

部署脚本会读取 `.env` 中的 `DEPLOY_RUNTIME`、`DEPLOY_TIMEOUT_SECONDS` 和 `TEI_GPU_IMAGE`；命令行参数和系统环境变量优先级更高。

可显式指定运行时：

```bash
./scripts/deploy.sh --runtime gpu
./scripts/deploy.sh --runtime cpu
```

GPU 模式无法通过预检会直接失败；自动模式则回退到 CPU 模式。启动完成前，脚本会在后端容器中调用一次真实 embedding 请求，并验证向量维度为 1024。任何步骤失败都会输出容器状态和最近日志。

生产环境应固定 `TEI_GPU_IMAGE` 到经过验证的镜像 digest，并将 `EMBEDDING_MODEL` 固定到模型 revision。驱动、Docker GPU 运行时、内存或磁盘等宿主机条件不满足时，脚本会选择 CPU 或明确失败，不会静默修改模型。

Docker Desktop/WSL 的 GPU 配置默认会预加载 `/usr/lib/x86_64-linux-gnu/libcuda.so.1`。其他宿主机若不需要该兼容设置，可将 `TEI_GPU_LD_PRELOAD` 置空。
