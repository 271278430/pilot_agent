# pilot_delivery

模型引擎、预览和交付协议。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [capabilities.py](capabilities.py) | capabilities相关实现。 |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [engine.py](engine.py) | Trusted model worker. No repository code is executed in the GPU process. |
| [engine_client.py](engine_client.py) | Bounded transport shared by delivery execution and read-only inventory. |
| [preview_contract.py](preview_contract.py) | Versioned interactive generation, separate from the frozen delivery API v1. |
| [preview_engine.py](preview_engine.py) | Trusted v2 interactive worker; old accepted v1 engine remains untouched. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
