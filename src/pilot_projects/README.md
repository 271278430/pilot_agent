# pilot_projects

项目登记、资源预检、源码快照和模型接入。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [api_engine.py](api_engine.py) | Trusted API worker; generated apps receive a socket, never the API key. |
| [contracts.py](contracts.py) | 协议相关实现。 |
| [github.py](github.py) | Public GitHub source admission. Clone code as data; never run hooks or installers. |
| [resources.py](resources.py) | Read hardware availability; estimates never reserve or select shared GPUs. |
| [snapshot.py](snapshot.py) | Bounded official codeload ZIP extraction, pinned to the GitHub commit API. |
| [store.py](store.py) | Business metadata points to official Workspace identities; source files stay owned. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
