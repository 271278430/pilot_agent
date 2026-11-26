# repopilot

仓库修复实验、检索、工具和验收入口。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [acceptance.py](acceptance.py) | 验收相关实现。 |
| [baseline.py](baseline.py) | Optional upstream mini-SWE baseline; never used as the primary framework. |
| [cli.py](cli.py) | 命令行相关实现。 |
| [demo.py](demo.py) | demo相关实现。 |
| [environment.py](environment.py) | 环境相关实现。 |
| [evaluation.py](evaluation.py) | 评测相关实现。 |
| [harness.py](harness.py) | Harness相关实现。 |
| [mcp_server.py](mcp_server.py) | Repo MCP adapter; official SDK owns lifecycle, framing and cancellation. |
| [mock_model.py](mock_model.py) | Explicit scripted Chat Completions server for real-Harness integration tests. |
| [models.py](models.py) | models相关实现。 |
| [repository.py](repository.py) | repository相关实现。 |
| [retrieval.py](retrieval.py) | 检索相关实现。 |
| [runtime_control.py](runtime_control.py) | Cooperative cancellation through the public SDK lifecycle, never process killing. |
| [storage.py](storage.py) | 存储相关实现。 |
| [tools.py](tools.py) | 工具相关实现。 |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
