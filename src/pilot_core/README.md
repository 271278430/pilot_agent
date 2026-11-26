# pilot_core

配置、模型协议、执行隔离与公共存储。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [capabilities.py](capabilities.py) | capabilities相关实现。 |
| [config.py](config.py) | Shared model credentials; no legacy Web or Session driver imports. |
| [context.py](context.py) | 上下文相关实现。 |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [docker.py](docker.py) | Trusted Docker access; privileged credentials never enter a workload container. |
| [execution_sandbox.py](execution_sandbox.py) | Trusted Docker broker. Workload code receives only a private container and inputs. |
| [harness_config.py](harness_config.py) | Harness 配置相关实现。 |
| [knowledge.py](knowledge.py) | Incremental, task-scoped retrieval for documents, source, and experiment records. |
| [mcp_server.py](mcp_server.py) | Official MCP lifecycle; business contracts and authorization remain local. |
| [model_protocol.py](model_protocol.py) | Shared Z.ai wire compatibility; model changes must reach routing and execution. |
| [policy.py](policy.py) | 策略相关实现。 |
| [resource_paths.py](resource_paths.py) | Managed download roots. Changing defaults never relocates existing artifacts. |
| [sandbox_askpass.py](sandbox_askpass.py) | Sudo's private authentication callback. Never invoke as a user-facing tool. |
| [sandbox_stdio.py](sandbox_stdio.py) | Private stdio broker used only by the native MCP client. Never executes workload code on host. |
| [storage.py](storage.py) | Project-owned runtime paths and process environment policy. |
| [storage_housekeeping.py](storage_housekeeping.py) | Owner housekeeping, separated from runtime path and environment policy. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
