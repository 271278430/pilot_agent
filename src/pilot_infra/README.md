# pilot_infra

服务器、资产、任务、节点执行、源码构建与推理验收。

## 文件

| 文件 | 内容 |
| --- | --- |
| [README.md](README.md) | 本目录的用途和文件索引。 |
| [__init__.py](__init__.py) | Python 包入口。 |
| [architecture.json](architecture.json) | 系统组件、关系与模块图的同源数据。 |
| [architecture.py](architecture.py) | Validated, read-only architecture catalogue shared by Web and documentation. |
| [asset_service.py](asset_service.py) | Node asset registration, persistent preparation and compatibility projection. |
| [benchmark.py](benchmark.py) | Frozen synthetic input policy shared across candidates and engine protocols. |
| [build_contracts.py](build_contracts.py) | Reviewed isolated environment/source work, separate from serving acceptance. |
| [build_service.py](build_service.py) | Durable reviewed source/environment workbench, with pinned SSH executors. |
| [build_validation.py](build_validation.py) | Candidate-scoped evidence; build and restoration never attest service quality. |
| [build_worker.py](build_worker.py) | Standalone Python 3.10 worker. Guest code never runs in the host interpreter. |
| [compatibility_sources.py](compatibility_sources.py) | Bounded domain references. Retrieved text is evidence, never authority. |
| [container_runtime.py](container_runtime.py) | Frozen OCI runtime contract. Never install host software or fall back to a process. |
| [contracts.py](contracts.py) | Domain requests, independent of the official Session/turn lifecycle. |
| [diagram.py](diagram.py) | Orthogonal architecture drawings, shared by Web and standalone SVG files. |
| [distributed_contracts.py](distributed_contracts.py) | Explicit, bounded two-node inference topology; saving it does not run it. |
| [distributed_recipes.py](distributed_recipes.py) | Fixed upstream command vectors for reviewed two-node recipes; never execute shell. |
| [distributed_service.py](distributed_service.py) | Reviewable two-node topology and evidence-based admission checks. |
| [docker_inventory.py](docker_inventory.py) | Read-only Docker inventory; secrets and arbitrary daemon commands are excluded. |
| [engine.py](engine.py) | Trusted SGLang recipes and independent tests. No agent supplied shell code. |
| [engine_admission.py](engine_admission.py) | Trusted pre-import gate. Its read-only directory is writable only by the Host. |
| [engine_launch.py](engine_launch.py) | Trusted entry point outside editable engine sources, mounted read-only in OCI. |
| [engine_source.py](engine_source.py) | Task-bound engine source and standard wheel artifacts; no Agent shell. |
| [fleet_contracts.py](fleet_contracts.py) | Owner-reviewed node assets and extensible compatibility contracts. |
| [fleet_transport.py](fleet_transport.py) | Fixed node protocol over the existing strict SSH access chain. |
| [fleet_worker.py](fleet_worker.py) | Portable, standard-library-only node worker. No LLM and no arbitrary shell. |
| [inference_http.py](inference_http.py) | Cancellable inference streams with a wall-clock bound, independent of the Agent loop. |
| [intent_catalog.json](intent_catalog.json) | 业务意图及动作分类目录。 |
| [metrics.py](metrics.py) | Client-observed inference measurements; target attainment stays separate. |
| [network_service.py](network_service.py) | Reviewable multi-node TCP tasks with bounded effects and explicit restoration. |
| [network_worker.py](network_worker.py) | Bounded TCP echo checks in a task-owned directory; no host/network reconfiguration. |
| [node_authority.py](node_authority.py) | One operation policy for assets, containers, software and node tests. |
| [node_docker.py](node_docker.py) | Per-node Docker credentials stay outside persistent job payloads. |
| [node_storage.py](node_storage.py) | Read-only Linux node preflight, also embedded in the fixed SSH protocol. |
| [pd_template.py](pd_template.py) | Locked vLLM 0.8.5 SharedStorageConnector verification, same-node 1P1D only. |
| [presets.py](presets.py) | Infra roles composed with the installed official Preset/tool APIs. |
| [process_engine.py](process_engine.py) | Task-owned GPU process backend for a host without NVIDIA Docker runtime. |
| [registered_workbench.py](registered_workbench.py) | Source work uses the registered node connection, roots and frozen scope. |
| [remote_engine.py](remote_engine.py) | Remote lifecycle with local independent protocol tests through a scoped tunnel. |
| [resource_assessment.py](resource_assessment.py) | Evidence-based capacity analysis, never a grant or an installation claim. |
| [resource_contracts.py](resource_contracts.py) | Server identity, owner authority and reviewable machine decisions. |
| [resource_probe.py](resource_probe.py) | A fixed read-only probe, runnable locally or through an admitted SSH channel. |
| [resource_registry.py](resource_registry.py) | Owner-controlled access configuration; never includes plaintext secrets in views. |
| [resource_service.py](resource_service.py) | Resource decisions and bounded task authority shared by native tools and Web. |
| [resource_transport.py](resource_transport.py) | Structured SSH chains: exact host keys, private credentials, fixed probe code. |
| [routing.py](routing.py) | Evidence admission only; native Sessions own expert execution and history. |
| [service.py](service.py) | One machine domain service shared by official tools and Web RPC. |
| [software_templates.py](software_templates.py) | Reviewed seed and independent checks, separate from candidate source. |
| [source_archive.py](source_archive.py) | Persist verified immutable source in the Host data workspace before restoration. |
| [stack_catalog.py](stack_catalog.py) | Evidence-labelled hard filtering; a vendor label never proves execution. |
| [store.py](store.py) | Transactional business state; full conversations remain in official Sessions. |
| [usage_guide.json](usage_guide.json) | 业务入口、步骤和权限说明的同源目录。 |
| [usage_guide.py](usage_guide.py) | One business guide shared by the settings page and generated Markdown. |
| [vllm_compat.py](vllm_compat.py) | Owner image-build compatibility for vLLM 0.8.5's numeric-only NVML helper. |
| [worker.py](worker.py) | Bounded domain Job runner; never invokes an LLM or writes Session history. |

目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。
