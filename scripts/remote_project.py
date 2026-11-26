"""Project-scoped SSH operations; never print credentials or delete outside this root."""

import argparse
from pathlib import Path

import paramiko
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/data/pilot-owner/pilot"


def connect():
    saved = dotenv_values(ROOT / ".local/ssh.env")
    config = {name: saved.get(name) or saved.get("INFRA_" + name) for name in ("SSH_HOST", "SSH_PORT", "SSH_USER", "SSH_PASSWORD")}
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.load_host_keys(str(ROOT / ".local/known_hosts"))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(config["SSH_HOST"], port=int(config["SSH_PORT"]), username=config["SSH_USER"],
                   password=config["SSH_PASSWORD"], timeout=15, banner_timeout=15, auth_timeout=15)
    return client, config


def command(client, text, password=None):
    stdin, stdout, stderr = client.exec_command(text, timeout=300)
    if password:
        stdin.write(password + "\n")
        stdin.flush()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    for key in ("REPOPILOT_API_KEY",):
        value = dotenv_values(ROOT / ".local/glm.env").get(key)
        if value:
            output = output.replace(value, "[REDACTED]")
            error = error.replace(value, "[REDACTED]")
    print(output[-12000:])
    if error:
        print(error[-3000:])
    if status:
        raise RuntimeError(f"Remote command exit={status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["command"], help="Source release uses scripts/deploy.py")
    parser.add_argument("--command-file", type=Path)
    parser.add_argument("--sudo", action="store_true")
    args = parser.parse_args()
    client, config = connect()
    try:
        if not args.command_file:
            raise ValueError("Use a reviewable command file")
        command(client, args.command_file.read_text(encoding="utf-8"), config["SSH_PASSWORD"] if args.sudo else None)
    finally:
        client.close()


if __name__ == "__main__":
    main()
