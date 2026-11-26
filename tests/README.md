# tests

Python 和 Node 回归测试、夹具与业务覆盖索引。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [brand_version.test.mjs](brand_version.test.mjs) | 验证界面标识 版本的交互或插件行为。 |
| [budget_reservations.test.mjs](budget_reservations.test.mjs) | 验证预算 预留的交互或插件行为。 |
| [build_context.test.mjs](build_context.test.mjs) | 验证构建 上下文的交互或插件行为。 |
| [business_intent.test.mjs](business_intent.test.mjs) | 验证业务 意图的交互或插件行为。 |
| [business_test_map.json](business_test_map.json) | 业务功能到测试文件的机器可读映射。 |
| [BUSINESS_TEST_MATRIX.md](BUSINESS_TEST_MATRIX.md) | 业务功能与相关测试脚本的映射。 |
| [conftest.py](conftest.py) | 为 pytest 提供共享配置和测试夹具。 |
| [domain_authorization.test.mjs](domain_authorization.test.mjs) | 验证领域 权限的交互或插件行为。 |
| [execution_sandbox.test.mjs](execution_sandbox.test.mjs) | 验证执行 沙箱的交互或插件行为。 |
| [expert_admission.test.mjs](expert_admission.test.mjs) | 验证专家 准入的交互或插件行为。 |
| [fleet_assets_ui.test.mjs](fleet_assets_ui.test.mjs) | 验证远端节点 资产 界面的交互或插件行为。 |
| [FULL_TEST_CATALOG.md](FULL_TEST_CATALOG.md) | 自动发现的 Python、Node 测试及辅助脚本目录。 |
| [history_isolation.test.mjs](history_isolation.test.mjs) | 验证历史记录 隔离的交互或插件行为。 |
| [host_settings.test.mjs](host_settings.test.mjs) | 验证主控 设置的交互或插件行为。 |
| [host_settings_ui.test.mjs](host_settings_ui.test.mjs) | 验证主控 设置 界面的交互或插件行为。 |
| [infra_interaction.test.mjs](infra_interaction.test.mjs) | 验证基础设施 交互的交互或插件行为。 |
| [intent_fixture.py](intent_fixture.py) | 为意图测试提供夹具。 |
| [mcp_fixture.py](mcp_fixture.py) | 为MCP测试提供夹具。 |
| [native_multimodal_probe.mjs](native_multimodal_probe.mjs) | 原生 多模态 探测相关的 Node 模块。 |
| [native_scope_probe.mjs](native_scope_probe.mjs) | 原生 范围 探测相关的 Node 模块。 |
| [node_business_ui.test.mjs](node_business_ui.test.mjs) | 验证节点 业务 界面的交互或插件行为。 |
| [plan_result_ui.test.mjs](plan_result_ui.test.mjs) | 验证方案 结果 界面的交互或插件行为。 |
| [preview_transport.test.mjs](preview_transport.test.mjs) | 验证预览 传输的交互或插件行为。 |
| [request_intent.test.mjs](request_intent.test.mjs) | 验证请求 意图的交互或插件行为。 |
| [sandbox_mcp_fixture.py](sandbox_mcp_fixture.py) | 为沙箱 MCP测试提供夹具。 |
| [sandbox_stub.mjs](sandbox_stub.mjs) | 沙箱 替身相关的 Node 模块。 |
| [test_acceptance.py](test_acceptance.py) | 验证验收相关行为与边界。 |
| [test_app_completion.py](test_app_completion.py) | 验证应用 完成相关行为与边界。 |
| [test_app_memory.py](test_app_memory.py) | 验证应用 记忆相关行为与边界。 |
| [test_architecture.py](test_architecture.py) | 验证架构相关行为与边界。 |
| [test_asset_official_approval.py](test_asset_official_approval.py) | 验证资产 官方 审批相关行为与边界。 |
| [test_atomic_json.py](test_atomic_json.py) | 验证原子写入 json相关行为与边界。 |
| [test_budget_reservations.py](test_budget_reservations.py) | 验证预算 预留相关行为与边界。 |
| [test_build_validation.py](test_build_validation.py) | 验证构建 校验相关行为与边界。 |
| [test_build_workbench.py](test_build_workbench.py) | 验证构建 工作台相关行为与边界。 |
| [test_business_intent.py](test_business_intent.py) | 验证业务 意图相关行为与边界。 |
| [test_cli.py](test_cli.py) | 验证命令行相关行为与边界。 |
| [test_compaction_policy.py](test_compaction_policy.py) | 验证上下文压缩 策略相关行为与边界。 |
| [test_composition_settings_scope.py](test_composition_settings_scope.py) | 验证组合 设置 范围相关行为与边界。 |
| [test_container_acceptance_default.py](test_container_acceptance_default.py) | 验证容器 验收 默认值相关行为与边界。 |
| [test_container_build_recovery.py](test_container_build_recovery.py) | 验证容器 构建 恢复相关行为与边界。 |
| [test_container_runtime.py](test_container_runtime.py) | 验证容器 运行时相关行为与边界。 |
| [test_delivery_analysis.py](test_delivery_analysis.py) | 验证交付 分析相关行为与边界。 |
| [test_delivery_engine_assets.py](test_delivery_engine_assets.py) | 验证交付 引擎 资产相关行为与边界。 |
| [test_delivery_terminal.py](test_delivery_terminal.py) | 验证交付 终端相关行为与边界。 |
| [test_deployment_scope.py](test_deployment_scope.py) | 验证部署 范围相关行为与边界。 |
| [test_directory_readmes.py](test_directory_readmes.py) | 验证directory readmes相关行为与边界。 |
| [test_distributed_cpu_relay.py](test_distributed_cpu_relay.py) | 验证跨节点 CPU 中继相关行为与边界。 |
| [test_distributed_planning.py](test_distributed_planning.py) | 验证跨节点 规划相关行为与边界。 |
| [test_domain_evaluation.py](test_domain_evaluation.py) | 验证领域 评测相关行为与边界。 |
| [test_engine_lifecycle.py](test_engine_lifecycle.py) | 验证引擎 生命周期相关行为与边界。 |
| [test_engine_official_approval.py](test_engine_official_approval.py) | 验证引擎 官方 审批相关行为与边界。 |
| [test_engine_source.py](test_engine_source.py) | 验证引擎 源码相关行为与边界。 |
| [test_environment.py](test_environment.py) | 验证环境相关行为与边界。 |
| [test_execution_sandbox.py](test_execution_sandbox.py) | 验证执行 沙箱相关行为与边界。 |
| [test_expert_routing.py](test_expert_routing.py) | 验证专家 路由相关行为与边界。 |
| [test_failed_engine_logs.py](test_failed_engine_logs.py) | 验证失败 引擎 日志相关行为与边界。 |
| [test_fleet_assets.py](test_fleet_assets.py) | 验证远端节点 资产相关行为与边界。 |
| [test_fleet_worker.py](test_fleet_worker.py) | 验证远端节点 执行器相关行为与边界。 |
| [test_full_project_check.py](test_full_project_check.py) | 验证完整 项目 检查相关行为与边界。 |
| [test_git_environment.py](test_git_environment.py) | 验证Git 环境相关行为与边界。 |
| [test_goal_applications.py](test_goal_applications.py) | 验证目标 应用相关行为与边界。 |
| [test_gpu_occupancy.py](test_gpu_occupancy.py) | 验证GPU 占用相关行为与边界。 |
| [test_harness_host.py](test_harness_host.py) | 验证Harness 主控相关行为与边界。 |
| [test_harness_integration.py](test_harness_integration.py) | 验证Harness 集成相关行为与边界。 |
| [test_host_settings.py](test_host_settings.py) | 验证主控 设置相关行为与边界。 |
| [test_host_settings_contract.py](test_host_settings_contract.py) | 验证主控 设置 协议相关行为与边界。 |
| [test_inference_controls.py](test_inference_controls.py) | 验证推理 控制相关行为与边界。 |
| [test_inference_metrics.py](test_inference_metrics.py) | 验证推理 指标相关行为与边界。 |
| [test_infra.py](test_infra.py) | 验证基础设施相关行为与边界。 |
| [test_infra_benchmark.py](test_infra_benchmark.py) | 验证基础设施 基准测试相关行为与边界。 |
| [test_infra_download.py](test_infra_download.py) | 验证基础设施 下载相关行为与边界。 |
| [test_infra_interaction.py](test_infra_interaction.py) | 验证基础设施 交互相关行为与边界。 |
| [test_infra_official_host.py](test_infra_official_host.py) | 验证基础设施 官方 主控相关行为与边界。 |
| [test_infra_transport.py](test_infra_transport.py) | 验证基础设施 传输相关行为与边界。 |
| [test_intent_scope_host.py](test_intent_scope_host.py) | 验证意图 范围 主控相关行为与边界。 |
| [test_knowledge.py](test_knowledge.py) | 验证知识相关行为与边界。 |
| [test_live_app_acceptance.py](test_live_app_acceptance.py) | 验证实时 应用 验收相关行为与边界。 |
| [test_mcp_http.py](test_mcp_http.py) | 验证MCP HTTP相关行为与边界。 |
| [test_mcp_official_host.py](test_mcp_official_host.py) | 验证MCP 官方 主控相关行为与边界。 |
| [test_model_protocol.py](test_model_protocol.py) | 验证模型 协议相关行为与边界。 |
| [test_multimodal_host.py](test_multimodal_host.py) | 验证多模态 主控相关行为与边界。 |
| [test_native_history_isolation.py](test_native_history_isolation.py) | 验证原生 历史记录 隔离相关行为与边界。 |
| [test_node_business_management.py](test_node_business_management.py) | 验证节点 业务 管理相关行为与边界。 |
| [test_node_registration.py](test_node_registration.py) | 验证节点 登记相关行为与边界。 |
| [test_pilot_infra.py](test_pilot_infra.py) | 验证Pilot 基础设施相关行为与边界。 |
| [test_plan_result_ui.py](test_plan_result_ui.py) | 验证方案 结果 界面相关行为与边界。 |
| [test_plan_revision.py](test_plan_revision.py) | 验证方案 修订相关行为与边界。 |
| [test_preview_controls.py](test_preview_controls.py) | 验证预览 控制相关行为与边界。 |
| [test_preview_transport.py](test_preview_transport.py) | 验证预览 传输相关行为与边界。 |
| [test_process_cleanup.py](test_process_cleanup.py) | 验证进程 清理相关行为与边界。 |
| [test_project_snapshot.py](test_project_snapshot.py) | 验证项目 快照相关行为与边界。 |
| [test_projects.py](test_projects.py) | 验证项目相关行为与边界。 |
| [test_publication_check.py](test_publication_check.py) | 验证publication 检查相关行为与边界。 |
| [test_recovery.py](test_recovery.py) | 验证恢复相关行为与边界。 |
| [test_remote_fleet_lifecycle.py](test_remote_fleet_lifecycle.py) | 验证远端 远端节点 生命周期相关行为与边界。 |
| [test_request_intent_host.py](test_request_intent_host.py) | 验证请求 意图 主控相关行为与边界。 |
| [test_resource_authority.py](test_resource_authority.py) | 验证资源 授权相关行为与边界。 |
| [test_resource_official_approval.py](test_resource_official_approval.py) | 验证资源 官方 审批相关行为与边界。 |
| [test_resource_paths.py](test_resource_paths.py) | 验证资源 路径相关行为与边界。 |
| [test_resource_probe_stream.py](test_resource_probe_stream.py) | 验证资源 探测 流式输出相关行为与边界。 |
| [test_retrieval.py](test_retrieval.py) | 验证检索相关行为与边界。 |
| [test_runtime_python.py](test_runtime_python.py) | 验证运行时 Python相关行为与边界。 |
| [test_storage.py](test_storage.py) | 验证存储相关行为与边界。 |
| [test_tools.py](test_tools.py) | 验证工具相关行为与边界。 |
| [test_usage_guide.py](test_usage_guide.py) | 验证使用说明 指南相关行为与边界。 |
| [test_vllm_image_compat.py](test_vllm_image_compat.py) | 验证vLLM 镜像 兼容性相关行为与边界。 |
| [test_workspace_folders.py](test_workspace_folders.py) | 验证工作区 文件夹相关行为与边界。 |
| [tool_system.test.mjs](tool_system.test.mjs) | 验证工具 系统的交互或插件行为。 |
| [tool_system_ui.test.mjs](tool_system_ui.test.mjs) | 验证工具 系统 界面的交互或插件行为。 |
| [usage_guide.test.mjs](usage_guide.test.mjs) | 验证使用说明 指南的交互或插件行为。 |
| [workbench_owner_start.test.mjs](workbench_owner_start.test.mjs) | 验证工作台 所有者 启动的交互或插件行为。 |
| [workspace_folders.test.mjs](workspace_folders.test.mjs) | 验证工作区 文件夹的交互或插件行为。 |
| [workspace_picker.test.mjs](workspace_picker.test.mjs) | 验证工作区 选择器的交互或插件行为。 |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
