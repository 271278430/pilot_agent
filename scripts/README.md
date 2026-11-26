# scripts

启动、测试、文档生成及受限环境操作脚本。旧环境专用脚本需先按目标环境核对。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [analyze_delivery.py](analyze_delivery.py) | Analyze completed paired trials without changing the registered task or grader. |
| [audit_cleanup.py](audit_cleanup.py) | Read-only Python dependency inventory for the supported Pilot entrypoints. |
| [benchmark_infra.py](benchmark_infra.py) | Reproducible real-server task suite. Run from the project virtualenv. |
| [bootstrap_delivery_engine.py](bootstrap_delivery_engine.py) | Start only the project-owned, read-only model engine on an idle authorized GPU. |
| [bootstrap_model_preview.py](bootstrap_model_preview.py) | Launch v2 on an idle GPU; never replace or stop the accepted v1 engine. |
| [bootstrap_remote.py](bootstrap_remote.py) | Run only on the designated project server; record setup without host installs. |
| [business_test_matrix.py](business_test_matrix.py) | Validate and render the business-to-regression map against the live catalogue. |
| [check_publication.py](check_publication.py) | Check staged source files for common private material before publication. |
| [cleanup_project.py](cleanup_project.py) | Delete reviewed, unused project files only after verifying their saved preimage. |
| [cpu_ray_gateway.py](cpu_ray_gateway.py) | 隔离 CPU Ray 实验使用的受限网络中继原型。 |
| [deploy.py](deploy.py) | Manifest-based Pilot release with external preimages and guarded cleanup. |
| [domain_eval.py](domain_eval.py) | Owner CLI: freeze a protocol, run finite trials, inspect and export receipts. |
| [evaluate_infra_agents.py](evaluate_infra_agents.py) | Finite real-model evaluation through the installed official Web/Session APIs. |
| [evaluate_pilot_infra.py](evaluate_pilot_infra.py) | Finite real-GPU domain evaluations; orchestration here is deterministic. |
| [export_infra_evidence.py](export_infra_evidence.py) | Export isolated QA evidence without credentials, models or native Session storage. |
| [full_project_check.py](full_project_check.py) | Discover, catalogue and run the complete local Pilot regression suite. |
| [harness-host-rollout.sh](harness-host-rollout.sh) | Harness 主控 rollout操作脚本。 |
| [harness-ui-rollout.sh](harness-ui-rollout.sh) | Harness 界面 rollout操作脚本。 |
| [harness_host_live.py](harness_host_live.py) | Finite real-model probe of the deployed official Web, with private login. |
| [harness_host_login.py](harness_host_login.py) | One local browser redirect; keep the official login URL out of tool output. |
| [infra_remote_eval.py](infra_remote_eval.py) | Run one finite evaluation action in the isolated server QA checkout. |
| [local_entry.py](local_entry.py) | Loopback redirect to the official private owner URL; never print credentials. |
| [pilot.py](pilot.py) | Run project commands with data-disk paths before importing the product. |
| [prepare_docker_remote.py](prepare_docker_remote.py) | Build the project sandbox image without changing the host Docker configuration. |
| [prepare_ml_environment.py](prepare_ml_environment.py) | Provision the registered ML recipe inside the user-authorized root. |
| [prepare_wasi.py](prepare_wasi.py) | Install a pinned, digest-checked CPython WASI runtime inside this project. |
| [remote_project.py](remote_project.py) | Project-scoped SSH operations; never print credentials or delete outside this root. |
| [render_workflow.py](render_workflow.py) | 生成主页流程动画，需安装 Pillow 和中文字体。 |
| [stage_infra.py](stage_infra.py) | Copy source to an isolated QA root; never switch the user's running Host. |
| [start_local.ps1](start_local.ps1) | 启动 本地操作脚本。 |
| [stop_local.ps1](stop_local.ps1) | stop 本地操作脚本。 |
| [sync_infra_stage.py](sync_infra_stage.py) | Refresh the isolated Infra QA sources with a hash-checked tar frame. |
| [update_architecture.py](update_architecture.py) | Generate or check documentation from the current architecture catalogue. |
| [update_directory_readmes.py](update_directory_readmes.py) | 生成或检查各目录的 README 索引。 |
| [update_usage_guide.py](update_usage_guide.py) | Generate or check the current guide from the shared business catalogue. |
| [web_tunnel.py](web_tunnel.py) | Localhost-only forward to the project web; credentials stay in local config. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
