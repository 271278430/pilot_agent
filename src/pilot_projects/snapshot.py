"""Bounded official codeload ZIP extraction, pinned to the GitHub commit API."""

import hashlib
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

MAX_BYTES = 512 * 1024**2


def extract_archive(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("快照目录已存在，不能覆盖或重复提取")
    with zipfile.ZipFile(archive) as bundle:
        files = bundle.infolist()
        if len(files) > 25000 or sum(f.file_size for f in files) > MAX_BYTES:
            raise ValueError("源码快照超过512MB或25,000文件上限")
        normalized, root_name = [], None
        for info in files:
            part = PurePosixPath(info.filename)
            if (
                part.is_absolute()
                or ".." in part.parts
                or "\\" in info.filename
                or any(":" in p for p in part.parts)
                or not part.parts
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise ValueError("源码快照包含不安全路径或符号链接")
            root_name = root_name or part.parts[0]
            if part.parts[0] != root_name or ".git" in part.parts:
                raise ValueError("源码快照根目录或Git目录不合法")
            relative = Path(*part.parts[1:])
            if str(relative) != ".":
                normalized.append((info, relative))
        destination.mkdir()
        total, count = 0, 0
        for info, relative in normalized:
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("源码快照路径越界")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("xb") as sink:
                shutil.copyfileobj(source, sink, length=65536)
            os.chmod(target, 0o600)
            total += target.stat().st_size
            count += 1
    return total, count


def download(url, ref, destination):
    from .github import canonical_url

    destination = Path(destination)
    if destination.exists():
        raise ValueError("快照目录已存在，不覆盖或自动重复下载")
    owner, repo = canonical_url(url).split("/")[-2:]
    with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
        response = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{quote(ref or 'HEAD', safe='')}"
        )
        if response.status_code != 200:
            raise ValueError(
                f"GitHub 提交查询失败（HTTP {response.status_code}），请检查分支或API限额"
            )
        commit = response.json().get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("GitHub 没有返回可信提交ID")
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = destination.parent / (".source-" + uuid.uuid4().hex + ".zip")
        try:
            deadline, received, digest = time.monotonic() + 90, 0, hashlib.sha256()
            with client.stream(
                "GET", f"https://codeload.github.com/{owner}/{repo}/zip/{commit}"
            ) as stream:
                if stream.status_code != 200:
                    raise ValueError(f"GitHub 源码快照不可用（HTTP {stream.status_code}）")
                with archive.open("xb") as sink:
                    for block in stream.iter_bytes(65536):
                        received += len(block)
                        if received > MAX_BYTES or time.monotonic() > deadline:
                            raise ValueError("源码快照下载超过大小或时间上限")
                        digest.update(block)
                        sink.write(block)
            size, count = extract_archive(archive, destination)
            return {
                "commit": commit,
                "bytes": size,
                "files": count,
                "source_method": "github_snapshot",
                "archive_sha256": digest.hexdigest(),
                "git_history": False,
                "clone": "GitHub official source snapshot; no Git/LFS/submodules",
            }
        finally:
            # Only the exact temporary ZIP created above, inside the owned project, is removed.
            if archive.exists() and archive.resolve().parent == destination.parent.resolve():
                archive.unlink()
