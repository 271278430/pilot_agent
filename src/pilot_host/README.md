# pilot_host

官方 Host 装配、业务桥接和命令行入口。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [asset_cli.py](asset_cli.py) | Human Owner CLI for the same node registry used by Pilot Web. |
| [bridge.py](bridge.py) | JSON domain bridge. No agent loop, chat history writer or shell tool. |
| [business_cli.py](business_cli.py) | Owner CLI for reviewed software and network tasks in the same registries. |
| [cli.py](cli.py) | Launch the official Web profile with domain plugins and official Presets. |
| [compaction_policy.py](compaction_policy.py) | Model-aware settings for the official compactor; no custom summary or loop. |
| [settings_cli.py](settings_cli.py) | Owner CLI for the same settings coordinator as the Web. |
| [storage_cli.py](storage_cli.py) | Owner storage inspection and reversible housekeeping. |
| [tool_cli.py](tool_cli.py) | CLI and Web share the exact Node configuration validator and private file. |
| [workspace_cli.py](workspace_cli.py) | Human CLI for the live Host's native workspace registry, shared with Web. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
