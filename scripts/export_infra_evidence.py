"""Export isolated QA evidence without credentials, models or native Session storage."""

import io
import json
import tarfile

from remote_project import ROOT, connect


def main():
    stage = json.loads((ROOT / ".local/infra-stage.json").read_text())["stage"]
    assert stage.startswith("/data/pilot-owner/pilot/.local/infra-qa-")
    destination = ROOT / "reports/infra-specialization-20261119/evidence"
    destination.mkdir(parents=True, exist_ok=True)
    program = r'''import hashlib,json,tarfile
from pathlib import Path
r=Path(STAGE);names=[]
for name in ['.local/domain-evaluation.json','.local/infra-agent-evaluation.json','.local/infra-agent-traces','.local/infra-full-final.xml','.local/infra-assets.json','runs/pilot-infra']:
 p=r/name
 if p.is_dir():names += [str(f.relative_to(r)) for f in p.rglob('*') if f.is_file()]
 elif p.is_file():names.append(name)
names += [str(p.relative_to(r)) for p in (r/'runs/harness-policy').glob('*/*.json')]
manifest={}
with tarfile.open(r/'.local/infra-evidence.tar.gz','w:gz') as tar:
 for name in sorted(set(names)):
  p=r/name;assert p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(r)
  data=p.read_bytes();manifest[name]=hashlib.sha256(data).hexdigest();item=tarfile.TarInfo(name);item.size=len(data);tar.addfile(item,__import__('io').BytesIO(data))
 p=r/'.local/infra-evidence-manifest.json';p.write_text(json.dumps(manifest,indent=2));tar.add(p,arcname='manifest.json')
print(len(manifest))
'''.replace("STAGE", repr(stage))
    client, _ = connect()
    try:
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=90)
        stdin.write(program)
        stdin.channel.shutdown_write()
        output, error = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(error[-1000:])
        buffer = io.BytesIO()
        with client.open_sftp() as sftp:
            sftp.getfo(stage + "/.local/infra-evidence.tar.gz", buffer)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as tar:
            for item in tar:
                path = destination / item.name
                assert item.isfile() and path.resolve().is_relative_to(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(item).read())
        print(json.dumps({"files": int(output), "destination": str(destination)}))
    finally:
        client.close()


if __name__ == "__main__":
    main()
