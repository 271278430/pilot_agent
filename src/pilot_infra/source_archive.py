"""Persist verified immutable source in the Host data workspace before restoration."""

import base64
import hashlib
import io
import tarfile


def archive(builds, task, candidate):
    contents, total = {}, 0
    if len(candidate["files"]) > 256:
        raise ValueError("源码归档文件数量超过上限")
    with builds.remote(task) as remote:
        for name, expected in candidate["files"].items():
            data = bytearray()
            while True:
                part = remote.call(
                    "read_candidate",
                    {
                        "candidate_id": candidate["candidate_id"],
                        "path": name,
                        "offset": len(data),
                        "limit": 300000,
                    },
                )
                if part["sha256"] != expected or not 0 <= part["bytes_total"] <= 4000000:
                    raise ValueError("冻结源码与归档清单不一致")
                chunk = base64.b64decode(part["content_base64"], validate=True)
                if len(chunk) > 300000 or not chunk and len(data) < part["bytes_total"]:
                    raise ValueError("源码归档响应不完整")
                data.extend(chunk)
                if len(data) >= part["bytes_total"]:
                    break
            if len(data) != part["bytes_total"] or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("源码归档校验失败")
            total += len(data)
            if total > 4000000:
                raise ValueError("源码归档总大小超过上限")
            contents[name] = bytes(data)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, data in contents.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o600, 0
            bundle.addfile(member, io.BytesIO(data))
    path = builds.store.path(
        task["id"], "software-sources/" + candidate["candidate_id"] + ".tar.gz"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("源码归档路径不安全")
    temporary.write_bytes(output.getvalue())
    temporary.replace(path)
    return {
        "source_archive": str(path.relative_to(builds.root)),
        "source_archive_sha256": hashlib.sha256(output.getvalue()).hexdigest(),
        "source_archive_bytes": len(output.getvalue()),
    }


def download(builds, task, candidate):
    if not candidate.get("source_archive"):
        raise ValueError("此历史候选没有完整源码归档；已有记录仍保留")
    path = builds.store.path(
        task["id"], "software-sources/" + candidate["candidate_id"] + ".tar.gz"
    )
    if not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError("源码归档缺失或超过大小上限")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != candidate["source_archive_sha256"]:
        raise ValueError("源码归档内容已变化")
    return {
        "filename": "pilot-software-" + task["id"] + "-" + candidate["candidate_id"] + ".tar.gz",
        "encoding": "base64",
        "data": base64.b64encode(data).decode(),
        "sha256": candidate["source_archive_sha256"],
    }
