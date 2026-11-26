from pathlib import Path

from .contracts import Contract


class PolicyDecision(Contract):
    allowed: bool
    reason: str
    isolated_runner_verified: bool = False


def controller_policy(controller: str) -> PolicyDecision:
    if controller == "scripted":
        return PolicyDecision(allowed=True, reason="registered trusted fixture")
    if controller != "model":
        return PolicyDecision(allowed=False, reason="Unknown controller")
    from repopilot.storage import read_json

    from .docker import docker

    root = Path(__file__).resolve().parents[2]
    marker = root / '.local/isolation.json'
    try:
        evidence = read_json(marker)
        actual = docker(['image', 'inspect', evidence['image'], '--format', '{{.Id}}']).stdout.strip()
        verified = evidence['passed'] and evidence['root'] == str(root) and evidence['image_id'] == actual
        verified = verified and len(evidence['checks']) >= 8 and all(c['passed'] for c in evidence['checks'].values())
    except (OSError, ValueError, RuntimeError, KeyError):
        verified = False
    return PolicyDecision(allowed=bool(verified), isolated_runner_verified=bool(verified),
                          reason="Validated project Docker image" if verified else "Real-model collaboration requires a validated isolated runner")


def require_shell_runner(environment: str):
    if environment != "docker":
        raise ValueError("shell requires an isolated runner; local fixtures use search/read/fixed test")
