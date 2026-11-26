# pilot_apps

模型应用计划、工作区、生命周期和验收。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [acceptance.py](acceptance.py) | Trusted live-inference checks; generated application code cannot write receipts. |
| [backend.py](backend.py) | 后端相关实现。 |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [fixtures.py](fixtures.py) | 为s测试提供夹具。 |
| [lifecycle.py](lifecycle.py) | Independent artifact settlement after the official execution tree stops. |
| [memory.py](memory.py) | Role-specific factual context; full history remains in SQLite and Session logs. |
| [model_prepare.py](model_prepare.py) | Trusted data-only ModelScope download; no model repository code execution. |
| [roles.py](roles.py) | Pilot role personas and official tool names; no Session scheduler. |
| [service.py](service.py) | Role-scoped domain tools; official Harness owns reasoning and iteration. |
| [store.py](store.py) | 持久化相关实现。 |
| [ui_contract.py](ui_contract.py) | Reject native form submission unsupported by the opaque preview sandbox. |
| [workspace.py](workspace.py) | Bounded multi-file delivery workspaces; never run repository hooks. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
