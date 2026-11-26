# Pilot 全量测试脚本目录

此目录由 `python scripts/full_project_check.py --update-catalog` 从现有测试脚本生成。
执行 `python scripts/full_project_check.py` 自动发现并运行全部 Python 和 Node 测试，
同时检查 Ruff、插件语法、使用说明与架构；用 `--check-catalog` 检查目录是否过期。
业务功能与相关脚本的映射见 [业务测试对应表](BUSINESS_TEST_MATRIX.md)。

直接运行的脚本按现有文件自动发现，辅助脚本供它们调用，不单独当成测试运行。
下方 Python 数字是静态定义数量，参数化后的实际用例数以 pytest 结果为准。
自动化测试中的模拟和条件跳过不等于真实模型、GPU 或远端业务验收。

Python 测试脚本：82；Node 测试脚本：22；辅助脚本：7。

## Python 功能与业务回归

- [test_acceptance.py](test_acceptance.py)：4 个测试定义
- [test_app_completion.py](test_app_completion.py)：6 个测试定义
- [test_app_memory.py](test_app_memory.py)：3 个测试定义
- [test_architecture.py](test_architecture.py)：6 个测试定义；Architecture reads must preserve state, guard Owner access and stay current.
- [test_asset_official_approval.py](test_asset_official_approval.py)：1 个测试定义；Installed native Harness approvals, scripted model, no node/GPU workload.
- [test_atomic_json.py](test_atomic_json.py)：3 个测试定义
- [test_budget_reservations.py](test_budget_reservations.py)：2 个测试定义
- [test_build_validation.py](test_build_validation.py)：9 个测试定义；False success, candidate changes and cleanup have separate outcome semantics.
- [test_build_workbench.py](test_build_workbench.py)：15 个测试定义；Safety and recovery behavior; real S5000 builds have separate evidence.
- [test_business_intent.py](test_business_intent.py)：1 个测试定义；Read-only capability hints shared with the semantic route; no secret paths.
- [test_cli.py](test_cli.py)：2 个测试定义
- [test_compaction_policy.py](test_compaction_policy.py)：1 个测试定义；Small models retain headroom; long-history summaries cannot use a fixed 1K cap.
- [test_composition_settings_scope.py](test_composition_settings_scope.py)：1 个测试定义；Native scripted fixtures must not consume the user's live model catalogue.
- [test_container_acceptance_default.py](test_container_acceptance_default.py)：2 个测试定义
- [test_container_build_recovery.py](test_container_build_recovery.py)：1 个测试定义
- [test_container_runtime.py](test_container_runtime.py)：5 个测试定义；Container safety failures; these fixtures do not claim GPU runtime validation.
- [test_delivery_analysis.py](test_delivery_analysis.py)：2 个测试定义；Ensure the evidence report cannot hide failures or overstate certainty.
- [test_delivery_engine_assets.py](test_delivery_engine_assets.py)：2 个测试定义；Model version evidence covers the tokenizer assets actually used by inference.
- [test_delivery_terminal.py](test_delivery_terminal.py)：2 个测试定义
- [test_deployment_scope.py](test_deployment_scope.py)：2 个测试定义；Release manifests must reject user/model/private paths before SSH access.
- [test_directory_readmes.py](test_directory_readmes.py)：2 个测试定义；Directory inventories and Git exclusions must follow the published tree.
- [test_distributed_cpu_relay.py](test_distributed_cpu_relay.py)：3 个测试定义；Failure boundaries for the isolated experimental two-host CPU relay.
- [test_distributed_planning.py](test_distributed_planning.py)：25 个测试定义；Two-node topology and authorization preflight must fail closed.
- [test_domain_evaluation.py](test_domain_evaluation.py)：10 个测试定义；Independent scoring, immutable admissions and public evidence boundaries.
- [test_engine_lifecycle.py](test_engine_lifecycle.py)：5 个测试定义；Real Linux CPU process/sandbox lifecycle; these are not GPU accuracy tests.
- [test_engine_official_approval.py](test_engine_official_approval.py)：1 个测试定义；Locked Harness native approval outcomes with scripted model; no GPU build/run.
- [test_engine_source.py](test_engine_source.py)：13 个测试定义；Contract behavior fixtures; real sandbox/GPU evidence is recorded separately.
- [test_environment.py](test_environment.py)：3 个测试定义
- [test_execution_sandbox.py](test_execution_sandbox.py)：11 个测试定义；Broker contracts and filesystem attacks; real Docker evidence is separate.
- [test_expert_routing.py](test_expert_routing.py)：7 个测试定义；Routing and model projections; these are mechanism checks, not model scores.
- [test_failed_engine_logs.py](test_failed_engine_logs.py)：1 个测试定义
- [test_fleet_assets.py](test_fleet_assets.py)：15 个测试定义；Registration never implies execution; node operations retain exact ownership.
- [test_fleet_worker.py](test_fleet_worker.py)：4 个测试定义；Portable journal/manifest behavior, no actual GPU claims.
- [test_full_project_check.py](test_full_project_check.py)：5 个测试定义；The complete test entry discovers new scripts and detects a stale catalogue.
- [test_git_environment.py](test_git_environment.py)：1 个测试定义；Inherited Git config must remain valid after MCP secret filtering.
- [test_goal_applications.py](test_goal_applications.py)：8 个测试定义
- [test_gpu_occupancy.py](test_gpu_occupancy.py)：7 个测试定义；GPU query failures never masquerade as an empty device.
- [test_harness_host.py](test_harness_host.py)：7 个测试定义；Domain and real official Web behavior; scripted LLM is not a quality eval.
- [test_harness_integration.py](test_harness_integration.py)：6 个测试定义
- [test_host_settings.py](test_host_settings.py)：1 个测试定义；Actual installed Host registry, blank Sessions, CLI and cold restoration.
- [test_host_settings_contract.py](test_host_settings_contract.py)：5 个测试定义；Applied settings bridge, native profile layering and Owner preflight.
- [test_inference_controls.py](test_inference_controls.py)：6 个测试定义
- [test_inference_metrics.py](test_inference_metrics.py)：4 个测试定义
- [test_infra.py](test_infra.py)：10 个测试定义
- [test_infra_benchmark.py](test_infra_benchmark.py)：2 个测试定义
- [test_infra_download.py](test_infra_download.py)：6 个测试定义
- [test_infra_interaction.py](test_infra_interaction.py)：1 个测试定义；Check Infra RPC concurrency and retained native chat result presentation.
- [test_infra_official_host.py](test_infra_official_host.py)：1 个测试定义；Installed official Preset/Subagent/Web integration with a scripted model.
- [test_infra_transport.py](test_infra_transport.py)：2 个测试定义
- [test_intent_scope_host.py](test_intent_scope_host.py)：1 个测试定义；The pinned native stream gate settles scope notices without provider or tool work.
- [test_knowledge.py](test_knowledge.py)：2 个测试定义
- [test_live_app_acceptance.py](test_live_app_acceptance.py)：8 个测试定义
- [test_mcp_http.py](test_mcp_http.py)：1 个测试定义；Real Streamable HTTP discovery through the same official CLI client.
- [test_mcp_official_host.py](test_mcp_official_host.py)：1 个测试定义；Scripted model, genuine native Host/MCP/approvals; no GPU or paid model.
- [test_model_protocol.py](test_model_protocol.py)：2 个测试定义；Validate actual serialized semantic requests after changing model generation.
- [test_multimodal_host.py](test_multimodal_host.py)：1 个测试定义；Installed native Host: actual image bytes on the wire, isolation and restore.
- [test_native_history_isolation.py](test_native_history_isolation.py)：1 个测试定义；Actual native Web, persisted events and compaction; a scripted model only.
- [test_node_business_management.py](test_node_business_management.py)：10 个测试定义；Permission, frozen checks and recovery failures cannot become business success.
- [test_node_registration.py](test_node_registration.py)：11 个测试定义；Node storage and remote adapter boundaries, without GPU or real Docker.
- [test_pilot_infra.py](test_pilot_infra.py)：29 个测试定义；Business behavior fixtures; these are not actual SGLang/GPU evaluations.
- [test_plan_result_ui.py](test_plan_result_ui.py)：1 个测试定义；Run the thin UI plugin's history and delivery-action lifecycle checks.
- [test_plan_revision.py](test_plan_revision.py)：9 个测试定义；Plan changes use the same domain operation from chat and Web, with no execution.
- [test_preview_controls.py](test_preview_controls.py)：2 个测试定义
- [test_preview_transport.py](test_preview_transport.py)：3 个测试定义
- [test_process_cleanup.py](test_process_cleanup.py)：3 个测试定义
- [test_project_snapshot.py](test_project_snapshot.py)：4 个测试定义
- [test_projects.py](test_projects.py)：14 个测试定义；Project admission, persistent identity and actual model-selection boundaries.
- [test_publication_check.py](test_publication_check.py)：2 个测试定义；The publication guard detects private paths and credential patterns.
- [test_recovery.py](test_recovery.py)：5 个测试定义
- [test_remote_fleet_lifecycle.py](test_remote_fleet_lifecycle.py)：5 个测试定义；Lifecycle effects are owned, bounded and recoverable; scripted GPU reads.
- [test_request_intent_host.py](test_request_intent_host.py)：1 个测试定义；Adversarial scripted model over the installed native tool admission pipeline.
- [test_resource_authority.py](test_resource_authority.py)：18 个测试定义；Behavioral authorization and machine decisions, without real GPU effects.
- [test_resource_official_approval.py](test_resource_official_approval.py)：1 个测试定义；Real installed Harness turn/approval/preset integration; scripted model, no GPU.
- [test_resource_paths.py](test_resource_paths.py)：2 个测试定义
- [test_resource_probe_stream.py](test_resource_probe_stream.py)：1 个测试定义；A remote exit status is not a promise that stdout has reached EOF.
- [test_retrieval.py](test_retrieval.py)：4 个测试定义
- [test_runtime_python.py](test_runtime_python.py)：2 个测试定义
- [test_storage.py](test_storage.py)：5 个测试定义
- [test_tools.py](test_tools.py)：8 个测试定义
- [test_usage_guide.py](test_usage_guide.py)：5 个测试定义；Guide availability is a read projection, not execution or a grant.
- [test_vllm_image_compat.py](test_vllm_image_compat.py)：1 个测试定义
- [test_workspace_folders.py](test_workspace_folders.py)：2 个测试定义；Actual installed Host registry, blank Sessions, CLI and cold restoration.

## Node 交互与插件回归

- [brand_version.test.mjs](brand_version.test.mjs)
- [budget_reservations.test.mjs](budget_reservations.test.mjs)
- [build_context.test.mjs](build_context.test.mjs)
- [business_intent.test.mjs](business_intent.test.mjs)
- [domain_authorization.test.mjs](domain_authorization.test.mjs)
- [execution_sandbox.test.mjs](execution_sandbox.test.mjs)
- [expert_admission.test.mjs](expert_admission.test.mjs)
- [fleet_assets_ui.test.mjs](fleet_assets_ui.test.mjs)
- [history_isolation.test.mjs](history_isolation.test.mjs)
- [host_settings.test.mjs](host_settings.test.mjs)
- [host_settings_ui.test.mjs](host_settings_ui.test.mjs)
- [infra_interaction.test.mjs](infra_interaction.test.mjs)
- [node_business_ui.test.mjs](node_business_ui.test.mjs)
- [plan_result_ui.test.mjs](plan_result_ui.test.mjs)
- [preview_transport.test.mjs](preview_transport.test.mjs)
- [request_intent.test.mjs](request_intent.test.mjs)
- [tool_system.test.mjs](tool_system.test.mjs)
- [tool_system_ui.test.mjs](tool_system_ui.test.mjs)
- [usage_guide.test.mjs](usage_guide.test.mjs)
- [workbench_owner_start.test.mjs](workbench_owner_start.test.mjs)
- [workspace_folders.test.mjs](workspace_folders.test.mjs)
- [workspace_picker.test.mjs](workspace_picker.test.mjs)

## 测试辅助脚本

- [conftest.py](conftest.py)
- [intent_fixture.py](intent_fixture.py)
- [mcp_fixture.py](mcp_fixture.py)
- [native_multimodal_probe.mjs](native_multimodal_probe.mjs)
- [native_scope_probe.mjs](native_scope_probe.mjs)
- [sandbox_mcp_fixture.py](sandbox_mcp_fixture.py)
- [sandbox_stub.mjs](sandbox_stub.mjs)
