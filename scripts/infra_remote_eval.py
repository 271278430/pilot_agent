"""Run one finite evaluation action in the isolated server QA checkout."""

import argparse
import json
import shlex

from remote_project import ROOT, command, connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    args = parser.parse_args()
    stage = json.loads((ROOT / ".local/infra-stage.json").read_text())["stage"]
    assert stage.startswith("/data/pilot-owner/pilot/.local/infra-qa-")
    client, _ = connect()
    try:
        agent_mode = args.mode.startswith("agent_")
        program = "evaluate_infra_agents.py" if agent_mode else "evaluate_pilot_infra.py"
        mode = args.mode.removeprefix("agent_") if agent_mode else args.mode
        with client.open_sftp() as sftp:
            with sftp.file(stage + "/scripts/" + program, "wb") as f:
                f.write((ROOT / "scripts" / program).read_bytes())
        command(
            client,
            "cd "
            + shlex.quote(stage)
            + "\nPYTHONPATH=src .venv/bin/python scripts/"
            + program
            + " "
            + shlex.quote(mode)
            + " --root "
            + shlex.quote(stage),
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
