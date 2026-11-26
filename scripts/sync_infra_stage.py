"""Refresh the isolated Infra QA sources with a hash-checked tar frame."""

import io
import json
import tarfile

from deploy import sources
from remote_project import ROOT, connect


def main():
    stage = json.loads((ROOT / ".local/infra-stage.json").read_text())["stage"]
    names = sources()
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        for name in names:
            data = (ROOT / name).read_bytes()
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    client, _ = connect()
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(60)
    try:
        with sftp.file(stage + ".tar.gz", "wb") as f:
            f.write(bundle.getvalue())
        # Sources only; QA state, assets, credentials and native Sessions stay.
        program = """import hashlib,json,tarfile
from pathlib import Path
stage=Path(STAGE)
assert stage.parent==Path('/data/pilot-owner/pilot/.local') and stage.name.startswith('infra-qa-')
expected=EXPECTED
count=0
with tarfile.open(str(stage)+'.tar.gz') as tar:
 for item in tar.getmembers():
  p=stage/item.name
  assert item.isfile() and item.name in expected and p.resolve().is_relative_to(stage)
  data=tar.extractfile(item).read();assert hashlib.sha256(data).hexdigest()==expected[item.name]
  if not p.exists() or p.read_bytes()!=data:
   p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);count+=1
print(json.dumps({'stage':str(stage),'changed':count}))
""".replace("STAGE", repr(stage)).replace("EXPECTED", repr(names))
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=90)
        stdin.write(program)
        stdin.channel.shutdown_write()
        output, error = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(error[:1000])
        print(output)
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    main()
