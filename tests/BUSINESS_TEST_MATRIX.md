# Pilot 业务与测试对应表

由使用说明中的功能目录和 `business_test_map.json` 生成。
运行 `python scripts/business_test_matrix.py --update` 更新，`--check` 校验功能和脚本一致。
这些关联表示相关自动化回归，不代表全部场景、真实GPU或模型业务已验收。

| 业务功能 | 相关测试脚本 |
|---|---|
| 业务意图识别与路由 | [business_intent.test.mjs](business_intent.test.mjs)、[request_intent.test.mjs](request_intent.test.mjs)、[test_request_intent_host.py](test_request_intent_host.py) |
| 工作区选择与新建文件夹 | [workspace_folders.test.mjs](workspace_folders.test.mjs)、[workspace_picker.test.mjs](workspace_picker.test.mjs)、[test_workspace_folders.py](test_workspace_folders.py) |
| 对话规划与方案编辑 | [test_pilot_infra.py](test_pilot_infra.py)、[test_plan_revision.py](test_plan_revision.py)、[infra_interaction.test.mjs](infra_interaction.test.mjs) |
| 部署并验证 | [test_pilot_infra.py](test_pilot_infra.py)、[test_container_runtime.py](test_container_runtime.py)、[test_remote_fleet_lifecycle.py](test_remote_fleet_lifecycle.py) |
| 故障诊断与受控修复 | [test_failed_engine_logs.py](test_failed_engine_logs.py)、[test_engine_source.py](test_engine_source.py)、[test_pilot_infra.py](test_pilot_infra.py) |
| 候选回归比较 | [test_inference_controls.py](test_inference_controls.py)、[test_inference_metrics.py](test_inference_metrics.py)、[test_pilot_infra.py](test_pilot_infra.py) |
| 质量与性能实验 | [test_inference_controls.py](test_inference_controls.py)、[test_inference_metrics.py](test_inference_metrics.py)、[test_pilot_infra.py](test_pilot_infra.py) |
| 服务器资源池与接入管理 | [test_node_registration.py](test_node_registration.py)、[test_resource_authority.py](test_resource_authority.py)、[node_business_ui.test.mjs](node_business_ui.test.mjs) |
| 整机观测与容量分析 | [test_gpu_occupancy.py](test_gpu_occupancy.py)、[test_resource_probe_stream.py](test_resource_probe_stream.py)、[test_resource_authority.py](test_resource_authority.py) |
| 权限模式、方案审批与撤销 | [test_resource_authority.py](test_resource_authority.py)、[test_resource_official_approval.py](test_resource_official_approval.py)、[domain_authorization.test.mjs](domain_authorization.test.mjs) |
| 引擎源码修改与制品交付 | [test_engine_source.py](test_engine_source.py)、[test_engine_official_approval.py](test_engine_official_approval.py)、[test_build_workbench.py](test_build_workbench.py) |
| 手动源码操作与交回智能体 | [test_engine_source.py](test_engine_source.py)、[test_infra_interaction.py](test_infra_interaction.py)、[infra_interaction.test.mjs](infra_interaction.test.mjs) |
| 实际回滚与重新验收 | [test_engine_source.py](test_engine_source.py)、[test_inference_controls.py](test_inference_controls.py) |
| 节点软件、评测与算子工作区 | [test_build_workbench.py](test_build_workbench.py)、[test_build_validation.py](test_build_validation.py)、[node_business_ui.test.mjs](node_business_ui.test.mjs)、[workbench_owner_start.test.mjs](workbench_owner_start.test.mjs) |
| 角色智能体与协作策略 | [test_expert_routing.py](test_expert_routing.py)、[expert_admission.test.mjs](expert_admission.test.mjs)、[test_harness_host.py](test_harness_host.py) |
| 暂停、继续、取消与服务清理 | [test_pilot_infra.py](test_pilot_infra.py)、[test_process_cleanup.py](test_process_cleanup.py)、[test_remote_fleet_lifecycle.py](test_remote_fleet_lifecycle.py) |
| 累计用量与预算恢复 | [test_budget_reservations.py](test_budget_reservations.py)、[budget_reservations.test.mjs](budget_reservations.test.mjs)、[test_pilot_infra.py](test_pilot_infra.py) |
| 原始证据、验收状态与报告下载 | [test_pilot_infra.py](test_pilot_infra.py)、[test_build_validation.py](test_build_validation.py)、[test_domain_evaluation.py](test_domain_evaluation.py) |
| 会话、任务记录与历史应用 | [history_isolation.test.mjs](history_isolation.test.mjs)、[test_native_history_isolation.py](test_native_history_isolation.py)、[test_harness_host.py](test_harness_host.py) |
| 历史评测与协作对照 | [test_domain_evaluation.py](test_domain_evaluation.py)、[test_inference_metrics.py](test_inference_metrics.py) |
| 界面设置、使用说明与项目架构 | [test_usage_guide.py](test_usage_guide.py)、[test_architecture.py](test_architecture.py)、[usage_guide.test.mjs](usage_guide.test.mjs) |
| 会话上下文圆环 | [test_compaction_policy.py](test_compaction_policy.py)、[test_native_history_isolation.py](test_native_history_isolation.py)、[host_settings_ui.test.mjs](host_settings_ui.test.mjs) |
| 工具系统与MCP配置 | [tool_system.test.mjs](tool_system.test.mjs)、[tool_system_ui.test.mjs](tool_system_ui.test.mjs)、[test_mcp_official_host.py](test_mcp_official_host.py) |
| 模型与镜像登记、下载和核验 | [test_fleet_assets.py](test_fleet_assets.py)、[test_asset_official_approval.py](test_asset_official_approval.py)、[fleet_assets_ui.test.mjs](fleet_assets_ui.test.mjs) |
| 厂商兼容栈筛选与配方扩展 | [test_fleet_assets.py](test_fleet_assets.py)、[test_vllm_image_compat.py](test_vllm_image_compat.py) |
| 远端持久容器执行、租约与复原 | [test_fleet_worker.py](test_fleet_worker.py)、[test_remote_fleet_lifecycle.py](test_remote_fleet_lifecycle.py)、[test_fleet_assets.py](test_fleet_assets.py) |
| 工作区、多个Pilot模型与资源路径 | [test_host_settings.py](test_host_settings.py)、[test_host_settings_contract.py](test_host_settings_contract.py)、[host_settings.test.mjs](host_settings.test.mjs)、[host_settings_ui.test.mjs](host_settings_ui.test.mjs) |
| 执行隔离与自检 | [test_execution_sandbox.py](test_execution_sandbox.py)、[execution_sandbox.test.mjs](execution_sandbox.test.mjs)、[test_mcp_official_host.py](test_mcp_official_host.py) |
| 图片输入与会话模型选择 | [test_multimodal_host.py](test_multimodal_host.py)、[test_model_protocol.py](test_model_protocol.py)、[host_settings.test.mjs](host_settings.test.mjs) |
| 主控与节点资源目录 | [test_storage.py](test_storage.py)、[test_resource_paths.py](test_resource_paths.py)、[test_node_registration.py](test_node_registration.py) |
| 跨机 PD / Ray 拓扑草稿与静态预检 | [test_distributed_planning.py](test_distributed_planning.py)、[test_distributed_cpu_relay.py](test_distributed_cpu_relay.py)、[node_business_ui.test.mjs](node_business_ui.test.mjs) |
| 多节点既有链路验证 | [test_node_business_management.py](test_node_business_management.py)、[node_business_ui.test.mjs](node_business_ui.test.mjs) |

## 实机和真实模型评测脚本

以下脚本可能产生真实模型费用、服务器或 GPU 副作用，不纳入默认回归；旧目录清理后不能直接重放。

| 脚本 | 范围和前提 |
|---|---|
| [scripts/domain_eval.py](../scripts/domain_eval.py) | Owner 领域评测编排；执行前需配置隔离环境和预算 |
| [scripts/evaluate_pilot_infra.py](../scripts/evaluate_pilot_infra.py) | 历史固定 GPU 业务验收；原 A100 资产已清理，须先重新配置 |
| [scripts/evaluate_infra_agents.py](../scripts/evaluate_infra_agents.py) | 历史真实模型 Agent 对照；须重新准备环境和预算 |
| [scripts/infra_remote_eval.py](../scripts/infra_remote_eval.py) | 历史 A100 QA 包装；依赖已清理的旧目录，不能直接使用 |
| [scripts/benchmark_infra.py](../scripts/benchmark_infra.py) | 历史真实服务器基准；硬编码的旧实验目录已清理 |
| [scripts/harness_host_live.py](../scripts/harness_host_live.py) | 真实模型/应用交付验证；会产生 API 和部署副作用 |

历史冻结协议与数据索引位于 `evals/`，验收范围见各次报告。
