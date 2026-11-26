"""Remote lifecycle with local independent protocol tests through a scoped tunnel."""

import base64
import contextlib
import select
import socketserver
import threading
import time
import uuid

from .asset_service import AssetService
from .engine import SGLangEngine, VLLMEngine
from .resource_registry import ResourceRegistry


class RemoteEngine(SGLangEngine):
    def __init__(self, registry, task, candidate):
        super().__init__(registry, task, candidate)
        self.assets = AssetService(registry.root)
        self.server = ResourceRegistry(registry.root).get(task["plan"]["machine_id"])
        self.executor = self.assets.executor(self.server["id"], cleanup=task.get("_cleanup", False))
        if task.get("_cleanup"):
            self.server = self.executor["server_snapshot"]
        self.tunnel = None
        self.local_endpoint = None
        self.stack = contextlib.ExitStack()

    def call(self, action, payload):
        return self.assets.transport.call(self.server, self.executor, action, payload)

    def operation(self, action, cancelled=lambda: False, extra=None):
        probe = self.call("probe", {})
        if self.executor.get("node_identity") and probe.get("node_identity") != self.executor["node_identity"]:
            raise ValueError("Registered node hardware identity changed")
        # A compensating stop is a different effect from the launch it cleans.
        # Its ID is stable across recovery, without reusing the launch journal.
        original = self.task.get("_operation_id")
        operation = (
            uuid.uuid5(uuid.NAMESPACE_URL, "pilot:" + original + ":" + action).hex
            if original
            else uuid.uuid4().hex
        )
        request = {
            "action": action,
            "task_id": self.task["id"],
            "candidate": self.candidate,
            "gpu_ids": self.task["plan"]["gpu_ids"],
            "model": self.model,
            "image": self.image,
            "recipe": self.image["stack_recipe"],
            "limits": self.limits(),
            "machine_identity": self.executor["machine_identity"],
            "boot_id": probe["boot_id"],
            "deadline": time.time() + 120
            if action == "stop"
            else self.task["state"].get("deadline_epoch", time.time() + 120),
        }
        request.update(extra or {})
        if action == "launch" and self.executor.get("cuda_runtime"):
            request["cuda_runtime"] = self.executor["cuda_runtime"]
        previous = self.call("lookup", {"operation_id": operation})
        if previous:
            comparable = {k: v for k, v in request.items() if k not in {"deadline", "boot_id"}}
            prior = {
                k: v for k, v in previous["request"].items() if k not in {"deadline", "boot_id"}
            }
            if comparable != prior:
                raise ValueError("Original remote operation has different effects")
        else:
            if action == "launch":
                from .fleet_transport import worker_current

                if not worker_current(self.executor):
                    raise ValueError("节点执行器版本需更新；先在空闲状态重新核验并准备")
            self.call("submit", {"operation_id": operation, "request": request})
        while True:
            row = self.call("status", {"operation_id": operation})
            if row["status"] == "completed":
                return row["result"]
            if row["status"] not in {"submitted", "running"}:
                raise ValueError(
                    row.get("error", "Original node operation requires reconciliation")
                )
            if action != "stop" and cancelled():
                self.call("cancel", {"operation_id": operation})
            time.sleep(0.3)

    def check_adapter(self, source):
        result = self.operation("adapter_check", extra={"adapter_source": base64.b64encode(source).decode()})
        if result.get("source_sha256") != self.candidate["source_sha256"] or result.get("image") != self.image["digest"]:
            raise ValueError("Remote adapter receipt differs from frozen candidate")
        from .fleet_worker import validate_adapter_options

        return validate_adapter_options(result["options"])

    def launch(self, cancelled=lambda: False):
        if not self.registered_identity():
            raise ValueError("Frozen remote assets changed")
        return self.operation("launch", cancelled)

    def verify_identity(self, ready=True):
        try:
            value = self.call("identity", {"task_id": self.task["id"]})
            if value.get("passed"):
                value["passed"] = bool(
                    value["config_digest"] == self.candidate["digest"]
                    and value["image"] == self.image["digest"]
                    and value["model_manifest"] == self.model["manifest_digest"]
                    and self.registered_identity()
                )
                if ready:
                    value["passed"] = bool(
                        value["passed"]
                        and self.call("manifest", {"path": self.model["path"]})["digest"]
                        == self.model["manifest_digest"]
                    )
            return value
        except (ValueError, OSError, RuntimeError):
            return {"passed": False, "error": "Remote identity cannot be verified"}

    def live_acceptance(self, accepted):
        value = self.verify_identity()
        return value.get("passed") and value == accepted

    def endpoint(self):
        if self.local_endpoint:
            return self.local_endpoint
        if self.tunnel:
            return "http://127.0.0.1:" + str(self.tunnel.server_address[1])
        value = self.verify_identity(ready=False)
        if not value.get("passed"):
            raise ValueError("Remote container identity unavailable")
        if self.server["connection"]["transport"] == "local":
            self.local_endpoint = "http://" + value["address"] + ":" + str(value["port"])
            return self.local_endpoint
        if self.tunnel is None:
            transport = self.stack.enter_context(
                self.assets.transport.transport.connect(self.server)
            )
            remote_port = value["port"]
            remote_address = value["address"]

            class Handler(socketserver.BaseRequestHandler):
                def handle(handler):
                    channel = transport.open_channel(
                        "direct-tcpip",
                        (remote_address, remote_port),
                        handler.request.getpeername(),
                        timeout=10,
                    )
                    try:
                        while True:
                            ready, _, _ = select.select([handler.request, channel], [], [], 15)
                            if not ready:
                                continue
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (channel if source is handler.request else handler.request).sendall(
                                    data
                                )
                    finally:
                        channel.close()

            class Tunnel(socketserver.ThreadingTCPServer):
                daemon_threads = True
                allow_reuse_address = False

            self.tunnel = Tunnel(("127.0.0.1", 0), Handler)
            threading.Thread(target=self.tunnel.serve_forever, daemon=True).start()
        return "http://127.0.0.1:" + str(self.tunnel.server_address[1])

    def stop(self):
        value = self.call("identity", {"task_id": self.task["id"]})
        original = value.get("launch_operation") or value.get("receipt", {}).get("labels", {}).get(
            "pilot.infra.launch"
        )
        if original:
            self.call("cancel", {"operation_id": original})
        return self.operation("stop")

    def logs(self):
        return self.call("logs", {"task_id": self.task["id"]})["text"]

    def performance_payload(self, length, workload):
        if self.image["engine"] == "vllm":
            return VLLMEngine.performance_payload(self, length, workload)
        return super().performance_payload(length, workload)

    def close(self):
        self.local_endpoint = None
        if self.tunnel:
            self.tunnel.shutdown()
            self.tunnel.server_close()
            self.tunnel = None
        self.stack.close()

    def test(self, adapter, cancelled=lambda: False, plan=None):
        self.endpoint()
        result = super().test(adapter, cancelled, plan)
        result["limits"].append(
            "Remote timings include the scoped SSH tunnel and network, not pure engine/kernel latency"
        )
        return result
