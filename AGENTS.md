# 开发约定

- 使用锁定版本的 DeepSeek Harness；优先复用官方 Session、Loop、Skill、MCP、审批和子会话能力。
- 业务状态、授权、候选身份、执行与独立验收由 Pilot 领域服务负责。未知副作用不得盲目重放。
- 不提交密钥、模型权重、工作区、运行日志、服务器私有配置或原始会话。
- 业务变化时维护 `src/pilot_infra/usage_guide.json`，运行 `python scripts/update_usage_guide.py`。
- 架构边界变化时维护 `src/pilot_infra/architecture.json`，运行 `python scripts/update_architecture.py`。
- 更新测试目录与业务映射，执行 `python scripts/full_project_check.py`。真实设备、模型和远端验证应与模拟测试区分。
- 重要计划与进展分别记录在 `docs/PLAN.md`、`docs/DEVELOPMENT_LOG.md` 和 `tasks/board.json`。
