# infra_pilot

独立的训练与推理环境实验入口，使用受限配方。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [agent.py](agent.py) | 智能体相关实现。 |
| [cli.py](cli.py) | 命令行相关实现。 |
| [controller.py](controller.py) | Deterministic, state-driven control provider for the official Harness. |
| [evaluation.py](evaluation.py) | Explicit test-only fault transport, selected only by a benchmark run config. |
| [local_transport.py](local_transport.py) | Same-host recipe transport: no SSH credential and no model-supplied commands. |
| [service.py](service.py) | 服务相关实现。 |
| [spec.py](spec.py) | spec相关实现。 |
| [transport.py](transport.py) | 传输相关实现。 |
| [worker.py](worker.py) | Standalone Python 3.10+ remote worker; no controller installation needed. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
