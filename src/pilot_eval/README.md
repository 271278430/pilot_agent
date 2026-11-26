# pilot_eval

领域评测、预算准入、评分和报告。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [approval_authorizer.mjs](approval_authorizer.mjs) | 审批 authorizer相关的 Node 模块。 |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [grader.py](grader.py) | Trusted extra probes against the exact service, outside editable candidates. |
| [problems.py](problems.py) | Versioned private fixtures; never registered as an Agent tool or workspace. |
| [report.py](report.py) | Public evaluation projection; never publish injection or reference material. |
| [runner.py](runner.py) | Finite trial scheduling; the installed Harness owns every Agent turn. |
| [store.py](store.py) | Private immutable manifests, serialized trial admission and durable phases. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
