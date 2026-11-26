"""Copy source to an isolated QA root; never switch the user's running Host."""

import io
import json
import tarfile
import uuid

from deploy import sources
from remote_project import REMOTE, ROOT, connect


def main():
    stage = REMOTE + "/.local/infra-qa-" + uuid.uuid4().hex[:10]
    bundle = io.BytesIO()
    names = sources()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        for name in names:
            path = ROOT / name
            item = tarfile.TarInfo(name)
            item.size = path.stat().st_size
            tar.addfile(item, io.BytesIO(path.read_bytes()))
    client, _ = connect()
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(60)
    try:
        remote_tar = stage + ".tar.gz"
        with sftp.file(remote_tar, "wb") as output:
            output.write(bundle.getvalue())
        program = """import os,shutil,tarfile
from pathlib import Path
root=Path('/data/pilot-owner/pilot');stage=Path(STAGE)
assert stage.parent==root/'.local' and not stage.exists()
stage.mkdir();(stage/'.local').mkdir()
with tarfile.open(str(stage)+'.tar.gz') as tar:
 for item in tar.getmembers():
  p=stage/item.name
  assert item.isfile() and p.resolve().is_relative_to(stage)
  p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(tar.extractfile(item).read())
(stage/'.venv').symlink_to(root/'.venv',target_is_directory=True)
(stage/'harness/runtime/node_modules').symlink_to(root/'harness/runtime/node_modules',target_is_directory=True)
for name in ['docker.env','glm.env']:
 if (root/'.local'/name).exists():
  shutil.copyfile(root/'.local'/name,stage/'.local'/name);os.chmod(stage/'.local'/name,0o600)
print(stage)
""".replace("STAGE", repr(stage))
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=60)
        stdin.write(program)
        stdin.channel.shutdown_write()
        out, err = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(err[:1000])
        print(out.strip())
        local = ROOT / ".local/infra-stage.json"
        local.write_text(json.dumps({"stage": stage}), encoding="utf-8")
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    main()
