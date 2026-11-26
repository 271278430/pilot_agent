# 开发与验证

执行完整回归：`python scripts/full_project_check.py`。只查看测试清单可使用 `--list`。

- 测试脚本变化后运行 `python scripts/full_project_check.py --update-catalog`。
- 业务映射变化后运行 `python scripts/business_test_matrix.py --update`。
- 业务使用说明以 `src/pilot_infra/usage_guide.json` 为源，运行 `python scripts/update_usage_guide.py` 更新文档。
- 架构目录以 `src/pilot_infra/architecture.json` 为源，运行 `python scripts/update_architecture.py` 更新图册和 SVG。
- 使用相应命令的 `--check` 核对生成文件是否同步。

运行数据、服务器配置、凭据、模型权重和实验报告不属于源码。复制或发布前应检查新文件及 Git 历史，避免将本机或服务器的私有数据带入仓库。
