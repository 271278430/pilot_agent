from pilot_infra.worker import run
from tests.test_pilot_infra import FakeEngine, create, setup, start


def test_launch_failure_keeps_original_log_after_container_cleanup(tmp_path):
    service = setup(tmp_path)
    task = start(service, create(service))
    candidate = service.candidate(
        task,
        {
            "request_id": "failed-log-candidate",
            "serving": task["plan"]["serving"],
            "reason": "原始启动故障不能被清理结果覆盖",
        },
    )
    job = service.submit(task, "launch", candidate["id"])

    class FailingEngine(FakeEngine):
        stopped = False

        def launch(self, cancelled):
            raise RuntimeError("warmup failed")

        def stop(self):
            self.stopped = True

        def logs(self):
            return (
                "Container absent" if self.stopped else "ROOT CAUSE: missing compiler before warmup"
            )

    result = run(tmp_path, job["job_id"], engine_factory=FailingEngine)
    assert not result["passed"] and result["error"] == "warmup failed"
    assert (
        service.store.path(task["id"], f"jobs/{job['job_id']}/engine.log").read_text()
        == "ROOT CAUSE: missing compiler before warmup"
    )
    assert service.store.operation(job["job_id"])["status"] == "failed"
