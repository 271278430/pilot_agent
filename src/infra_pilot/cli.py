import base64
import hashlib
import json
from pathlib import Path
from typing import Annotated

import paramiko
import typer
from dotenv import load_dotenv

from repopilot.harness import PROJECT
from repopilot.storage import read_json

from .agent import execute_infra, prepare_infra
from .spec import InfraSpec

app = typer.Typer(no_args_is_help=True, help="InfraPilot: audited CUDA training/inference environment agent")


@app.command()
def enroll(config: Annotated[Path, typer.Option("--config")], fingerprint: Annotated[str, typer.Option("--fingerprint")], known_hosts: Path = PROJECT / ".local/known_hosts"):
    """Pin the SSH host public key after checking a supplied SHA256 fingerprint."""
    spec = InfraSpec.model_validate(read_json(config))
    connection = paramiko.Transport((spec.host, spec.port))
    try:
        connection.start_client(timeout=20)
        key = connection.get_remote_server_key()
        actual = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if actual != fingerprint:
            raise typer.BadParameter("SSH public key does not match the supplied fingerprint")
        hosts = paramiko.HostKeys()
        if known_hosts.exists():
            hosts.load(str(known_hosts))
        hosts.add(spec.host if spec.port == 22 else f"[{spec.host}]:{spec.port}", key.get_name(), key)
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.save(str(known_hosts))
        typer.echo(json.dumps({"fingerprint": actual, "known_hosts": str(known_hosts)}))
    finally:
        connection.close()


@app.command()
def run(config: Annotated[Path, typer.Option("--config")], ssh_env: Path = PROJECT / ".local/ssh.env", known_hosts: Path = PROJECT / ".local/known_hosts", output: Path = PROJECT / "runs", controller: str = "rules", adopt_job: str | None = None):
    """Create a deployment workspace and run the official Harness workflow."""
    if controller not in {"rules", "llm"}:
        raise typer.BadParameter("controller must be rules or llm")
    load_dotenv(PROJECT / ".env")
    spec = InfraSpec.model_validate(read_json(config))
    location = prepare_infra(spec, output, ssh_env, known_hosts, adopt_job)
    typer.echo(f"Infra run: {location}")
    result = execute_infra(location, controller)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)


@app.command()
def resume(run_dir: Annotated[Path, typer.Option("--run-dir")], controller: str = "rules"):
    """Restore the native Session and inspect the existing journaled remote job."""
    load_dotenv(PROJECT / ".env")
    result = execute_infra(run_dir, controller, resume=True)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)


@app.command()
def status(run_dir: Annotated[Path, typer.Option("--run-dir")]):
    """Show durable local state without SSH or model calls."""
    typer.echo(json.dumps(read_json(run_dir / "checkpoint.json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
