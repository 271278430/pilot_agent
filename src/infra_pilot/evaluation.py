"""Explicit test-only fault transport, selected only by a benchmark run config.

No model or worker result is fabricated. Lost acknowledgements discard a real
launch reply; disconnection closes a real Paramiko transport. Native failures
use a job-local environment, never a host-wide configuration change.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from repopilot.storage import atomic_json, read_json

from . import service
from .transport import Remote


class FaultRemote(Remote):
    fixture: Path

    def record_fault(self, **details):
        state = read_json(self.fixture)
        state.update(triggered=True, triggered_at=time.time(), **details)
        atomic_json(self.fixture, state)

    def worker(self, *args):
        fixture = read_json(self.fixture)
        fault = fixture["scenario"]
        start = args[0] == "start"
        target = args[1] if start else None
        if start and fault == "port_conflict" and target == "serve_benchmark" and not fixture.get("triggered"):
            self.record_fault(job=args[2], stage=target, port=self.spec.service_port)
        if start and fault == "nccl_interface" and target == "nccl_smoke":
            self.record_fault(job=args[2], stage=target, interface="infrapilot_no_iface")
            return self.command(["env", "NCCL_SOCKET_IFNAME=infrapilot_no_iface", "NCCL_DEBUG=WARN", "python3", self.spec.remote + "/worker.py", self.spec.remote, *args], timeout=45)
        if start and fault == "cuda_oom" and target == "inference":
            self.record_fault(job=args[2], stage=target, allocator_fraction=0.0001)
            return self.command(["env", "INFRAPILOT_OOM=1", "PYTHONPATH=" + self.spec.remote + "/fault-hook", "python3", self.spec.remote + "/worker.py", self.spec.remote, *args], timeout=45)
        result = super().worker(*args)
        if start and target == "inference" and not fixture.get("triggered"):
            if fault == "lost_ack":
                self.record_fault(job=args[2], stage=target, actual_launch=result)
                raise ConnectionError("benchmark: discarded real launch acknowledgement")
            if fault == "ssh_drop":
                self.record_fault(job=args[2], stage=target, actual_launch=result)
                self.client.close()
        return result

    def wait(self, job, seconds=20):
        result = super().wait(job, seconds)
        if result.get("stage") == "serve_benchmark" and result.get("status") == "failed":
            self.fetch("logs/server.log", self.fixture.parent / "raw-benchmark" / (job + "-server.log"))
        return result


def main():
    run = Path(sys.argv[1]).resolve()
    config = read_json(run / "infra-run.json")
    if not config.get("benchmark"):
        raise ValueError("Fault instrumentation requires an explicit benchmark config")
    FaultRemote.fixture = run / "fault.json"
    if not FaultRemote.fixture.is_file():
        raise ValueError("Missing local fault fixture")
    service.Remote = FaultRemote
    service.serve_mcp(run)


if __name__ == "__main__":
    main()
