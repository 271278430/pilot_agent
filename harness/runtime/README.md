# runtime

固定版本的 Harness 运行时依赖及工作区适配。

## 子目录

| 目录 | 内容 |
| --- | --- |
| [plugins](plugins/README.md) | Pilot 的业务工具、策略和界面插件。 |

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [package-lock.json](package-lock.json) | Node 依赖的精确版本与完整性信息。 |
| [package.json](package.json) | Node 包信息、入口或依赖声明。 |
| [workspaces.mjs](workspaces.mjs) | workspaces相关的 Node 模块。 |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
