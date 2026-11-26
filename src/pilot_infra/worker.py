"""Bounded domain Job runner; never invokes an LLM or writes Session history."""

import hashlib
import json
import sys
import time

from pilot_core.docker import docker
from repopilot.storage import atomic_json

from .engine import machine_probe
from .fleet_worker import ADAPTER_RUNNER, validate_adapter_options
from .process_engine import engine_for
from .service import InfraService


def compact(result):
    if not isinstance(result, dict):
        return result
    value = {**result}
    if "artifact" in value:
        value["artifact"] = {k: v for k, v in value["artifact"].items() if k != "runtime_files"}
    if "quality" in value:
        value["quality"] = [{k: v for k, v in q.items() if k != "events"} for q in value["quality"]]
    if "cases" in value:
        value["cases"] = [
            {k: v for k, v in c.items() if k not in {"rows", "warmup"}} for c in value["cases"]
        ]
    return value


def adapter_options(service, owner, candidate):
    path = service.store.path(owner["id"], f"candidates/{candidate['id']}/adapter.py")
    source = path.read_bytes()
    if len(source) > 65536 or hashlib.sha256(source).hexdigest() != candidate["source_sha256"]:
        raise ValueError("Candidate source changed after freeze")
    assets = service.assets.read()
    machine = next((m for m in assets["machines"] if m["id"] == owner["plan"]["machine_id"]), None)
    if machine and machine.get("transport", "local") != "local":
        from .remote_engine import RemoteEngine

        remote = RemoteEngine(service.assets, owner, candidate)
        try:
            return remote.check_adapter(source)
        finally:
            remote.close()
    image = assets.get("adapter_image")
    if not image or not image.startswith("sha256:"):
        raise ValueError("Pinned CPU adapter sandbox image missing")
    result = docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:size=8m",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            str(path.parent) + ":/source:ro",
            image,
            "python",
            "-I",
            "-c",
            ADAPTER_RUNNER,
        ],
        timeout=20,
    )
    if len(result.stdout) > 16000:
        raise ValueError("Adapter output exceeded budget")
    # Revalidate output outside the untrusted sandbox. A module could bypass
    # sandbox-side asserts or print its own JSON; trusted client stays bounded.
    options = json.loads(result.stdout)
    return validate_adapter_options(options)


def run(root, operation_id, engine_factory=engine_for, adapter_loader=adapter_options):
    service = InfraService(root, spawn_jobs=False)
    store = service.store
    job = store.operation(operation_id)
    if job["status"] != "submitted":
        raise ValueError("Worker cannot replay an already claimed operation")
    task = store.get(job["data"]["consumer_task_id"])
    owner = store.get(job["task_id"])
    owner = {**owner, "_operation_id": operation_id,
             "_execution_deadline_epoch": task["state"].get("deadline_epoch")}
    candidate = next(
        c for c in owner["state"]["candidates"] if c["id"] == job["data"]["candidate_id"]
    )
    engine = None
    store.start_operation(operation_id)
    directory = store.path(task["id"], f"jobs/{operation_id}")
    directory.mkdir(parents=True, exist_ok=True)

    authority_lost = False

    def cancelled():
        nonlocal authority_lost
        current = store.get(task["id"])["state"]
        try:
            grant = service.resources.check_task(store.get(owner["id"]), job["action"], candidate)
            service.resources.check_usage(
                owner, grant or {"limits": service.resources.execution_limits(owner)}, engine
            )
        except ValueError:
            authority_lost = True
            return True
        return current["status"] != "running" or time.time() >= current.get("deadline_epoch", 0)

    result, state, failure_layer = None, "failed", "runtime"
    try:
        grant = service.resources.check_task(owner, job["action"], candidate)
        engine = engine_factory(
            service.assets,
            {
                **owner,
                "_cleanup": job["action"] == "stop",
                "_resource_limits": service.resources.execution_limits(owner, grant),
                "_operation_id": operation_id,
                "_execution_deadline_epoch": task["state"].get("deadline_epoch"),
            },
            candidate,
        )
        probe = (
            service.resources.transport.observe(
                service.resources.registry.get(owner["plan"]["machine_id"])
            )
            if getattr(engine, "image", {}).get("server_id")
            else machine_probe()
        )
        atomic_json(directory / "probe.json", probe)
        action = job["action"]
        if action == "stop":
            engine.stop()
            store.update(owner["id"], service_candidate=None)
            store.release(owner["id"])
            result = {"passed": True, "stopped": candidate["id"]}
        else:
            if cancelled():
                raise InterruptedError("Task stopped before operation")
            if action == "engine_build":
                failure_layer = "engine_build"
                from .engine_source import build_artifact

                service.resources.check_task(owner, "source_write", candidate)
                result = build_artifact(
                    service,
                    {**owner, "_resource_limits": service.resources.execution_limits(owner, grant)},
                    candidate,
                    operation_id,
                    cancelled,
                )
            elif action in {"launch", "rollback"}:
                failure_layer = "engine" if candidate.get("engine_source") else "runtime"
                previous = owner["state"].get("service_candidate")
                if previous and previous != candidate["id"]:
                    old = next(c for c in owner["state"]["candidates"] if c["id"] == previous)
                    engine_factory(service.assets, owner, old).stop()
                    store.update(owner["id"], service_candidate=None)
                result = engine.launch(cancelled)
                store.update(owner["id"], service_candidate=candidate["id"])
                if action == "rollback":
                    options = adapter_loader(service, owner, candidate)
                    result = engine.test(options, cancelled, task["plan"])
                    result.update(
                        job_id=operation_id,
                        observed_at=time.time(),
                        rollback={"from": previous, "to": candidate["id"], "retested": True},
                    )
            elif action == "adapter_check":
                failure_layer = "adapter"
                result = {
                    "passed": True,
                    "options": adapter_loader(service, owner, candidate),
                    "source_sha256": candidate["source_sha256"],
                }
            elif action == "test":
                if owner["state"].get("service_candidate") != candidate["id"]:
                    raise ValueError("Requested candidate is not the active target service")
                failure_layer = "adapter"
                options = adapter_loader(service, owner, candidate)
                failure_layer = "inference"
                result = engine.test(options, cancelled, task["plan"])
                result.update(job_id=operation_id, observed_at=time.time())
            else:
                raise ValueError("Unknown bounded worker action")
        if action != "stop" and cancelled():
            raise InterruptedError("Task stopped before result commit")
        state = "completed"
    except InterruptedError as exc:
        if (job["action"] in {"launch", "rollback"} or authority_lost) and engine is not None:
            current = store.get(owner["id"])["state"].get("service_candidate")
            cleanup = engine
            if current and current != candidate["id"]:
                prior = next(c for c in owner["state"]["candidates"] if c["id"] == current)
                cleanup = engine_factory(service.assets, {**owner, "_cleanup": True}, prior)
            try:
                cleanup.stop()
                store.update(owner["id"], service_candidate=None)
                store.release(owner["id"])
            except Exception as failure:
                state, result = (
                    "unknown",
                    {"passed": False, "error": str(exc), "cleanup_error": str(failure)},
                )
                store.update(
                    task["id"], status="paused", error="撤销后的自有进程未结算，需恢复核对。"
                )
        if state != "unknown":
            state, result = "cancelled", {"passed": False, "error": str(exc)}
    except Exception as exc:
        result = {"passed": False, "error": str(exc)[:3000], "failure_layer": failure_layer}
        if job["action"] == "engine_build":
            build = store.operation(operation_id)["data"]
            if build.get("build_container_name"):
                from .container_runtime import stop_owned

                try:
                    stop_owned(
                        build["build_container_name"],
                        build["build_labels"],
                        build.get("build_container_id"),
                    )
                except Exception as cleanup:
                    state = "unknown"
                    result["cleanup_error"] = str(cleanup)[:1000]
                    store.update(
                        task["id"],
                        status="paused",
                        error="构建容器副作用尚未结算；核对原Job后再继续。",
                    )
        if job["action"] in {"launch", "rollback"} and engine is not None:
            try:
                (directory / "engine.log").write_text(engine.logs(), encoding="utf-8")
                engine.stop()
                store.update(owner["id"], service_candidate=None)
            except Exception as cleanup:
                state = "unknown"
                result["cleanup_error"] = str(cleanup)[:1000]
                store.update(
                    task["id"],
                    status="paused",
                    error="启动失败且副作用尚未结算；核对原 Job 后再继续。",
                )
    finally:
        try:
            if job["action"] != "engine_build" and not (directory / "engine.log").is_file():
                (directory / "engine.log").write_text(engine.logs(), encoding="utf-8")
        except Exception:
            pass
        if engine is not None and hasattr(engine, "close"):
            engine.close()
        atomic_json(directory / "result.json", result)
        summary = compact(result)
        summary["evidence_sha256"] = hashlib.sha256(
            (directory / "result.json").read_bytes()
        ).hexdigest()
        if job["action"] in {"test", "rollback"} and state == "completed":
            if getattr(engine, "image", {}).get("server_id") and result.get("passed"):
                from .asset_service import AssetService

                try:
                    AssetService(root).record_runtime(
                        owner, candidate, result, summary["evidence_sha256"]
                    )
                except ValueError as exc:
                    # Evidence indexing cannot change the independent test result.
                    store.event(task["id"], "stack_evidence_index_failed", {"error": str(exc)})
            current = store.get(task["id"])
            store.update(
                task["id"], experiments=[*current["state"]["experiments"], summary], phase="review"
            )
        store.operation(operation_id, state, result=summary, ended_epoch=time.time())
        if state != "unknown":
            store.update(owner["id"], active_job=None)
            if task["id"] != owner["id"]:
                store.update(task["id"], active_job=None)
        if state != "unknown" and not store.get(owner["id"])["state"].get("service_candidate"):
            store.release(owner["id"])
        store.event(
            task["id"],
            "job_settled",
            {
                "id": operation_id,
                "action": job["action"],
                "candidate_id": candidate["id"],
                "status": state,
                "result": summary,
                "evidence": f"jobs/{operation_id}/result.json",
            },
        )
    return result


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
