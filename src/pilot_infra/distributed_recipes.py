"""Fixed upstream command vectors for reviewed two-node recipes; never execute shell."""

from .distributed_contracts import DistributedPlan


def render_commands(raw):
    plan = DistributedPlan.model_validate(raw)
    nodes = {node.role: node for node in plan.nodes}
    model = plan.served_model_name
    if plan.mode == "sglang_pd":
        prefill, decode = nodes["prefill"], nodes["decode"]
        commands = {}
        for node in (prefill, decode):
            commands[node.role] = [
                "python3", "-m", "sglang.launch_server", "--model-path", "/model",
                "--served-model-name", model, "--host", node.address, "--port", str(node.ports["api"]),
                "--disaggregation-mode", node.role, "--disaggregation-transfer-backend", "mooncake",
                "--disaggregation-bootstrap-port", str(prefill.ports["bootstrap"]),
                "--disaggregation-ib-device", node.rdma_device,
                "--tp-size", str(len(node.gpu_ids)), "--context-length", str(plan.context_length),
                "--max-running-requests", str(plan.max_concurrency), "--load-balance-method", "round_robin",
            ]
        commands["router"] = [
            "python3", "-m", "sglang_router.launch_router", "--pd-disaggregation",
            "--prefill", f"http://{prefill.address}:{prefill.ports['api']}", str(prefill.ports["bootstrap"]),
            "--decode", f"http://{decode.address}:{decode.ports['api']}",
            "--host", "127.0.0.1", "--port", str(prefill.ports["router"]),
        ]
        return commands

    head, worker = nodes["head"], nodes["worker"]

    def ray(node):
        ports = node.ports
        return [
            "python3", "-m", "ray.scripts.scripts", "start", "--block",
            "--node-ip-address", node.address, "--num-cpus", str(node.limits.cpu_cores),
            "--num-gpus", str(len(node.gpu_ids)), "--temp-dir", "/runtime/ray",
            "--node-manager-port", str(ports["node_manager"]),
            "--object-manager-port", str(ports["object_manager"]),
            "--dashboard-agent-listen-port", str(ports["agent_http"]),
            "--dashboard-agent-grpc-port", str(ports["agent_grpc"]),
            "--runtime-env-agent-port", str(ports["runtime_env"]),
            "--metrics-export-port", str(ports["metrics"]),
            "--worker-port-list", ",".join(str(ports[f"worker_{i}"]) for i in range(1, 5)),
        ]

    head_command = ray(head) + ["--head", "--port", str(head.ports["gcs"]),
                                "--include-dashboard", "false", "--dashboard-host", "127.0.0.1",
                                "--dashboard-port", str(head.ports["dashboard"]),
                                "--ray-client-server-port", str(head.ports["ray_client"])]
    worker_command = ray(worker) + ["--address", f"{head.address}:{head.ports['gcs']}"]
    count = sum(len(node.gpu_ids) for node in plan.nodes)
    if plan.mode == "sglang_ray":
        serve = ["python3", "-m", "sglang.launch_server", "--model-path", "/model",
                 "--served-model-name", model, "--host", "127.0.0.1", "--port", str(head.ports["api"]),
                 "--use-ray", "--nnodes", "2", "--tp-size", str(count), "--nccl-port", str(head.ports["nccl"]),
                 "--context-length", str(plan.context_length), "--max-running-requests", str(plan.max_concurrency)]
    else:
        serve = ["python3", "-m", "vllm.entrypoints.openai.api_server", "--model", "/model",
                 "--served-model-name", model, "--host", "127.0.0.1", "--port", str(head.ports["api"]),
                 "--distributed-executor-backend", "ray", "--tensor-parallel-size", str(count),
                 "--max-model-len", str(plan.context_length)]
    return {"head": head_command, "worker": worker_command, "serve": serve}
