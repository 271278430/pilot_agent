# pilot_collab

协作状态、历史记录与保留的实验协议。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [conversations.py](conversations.py) | Durable user conversations, separate from Agent messages and tool events. |
| [fixture.py](fixture.py) | 为目录测试提供夹具。 |
| [public_trace.py](public_trace.py) | Allowlisted user-facing facts; not raw logs, prompts or hidden reasoning. |
| [recipes.py](recipes.py) | Immutable self-built tasks; no reference patch or action sequence enters the workspace. |
| [routing.py](routing.py) | Finite intent classification and validated plan preview, without hidden execution. |
| [semantic.py](semantic.py) | Bounded semantic admission, independent of the official Harness execution loop. |
| [store.py](store.py) | 持久化相关实现。 |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
