"""Public GitHub source admission. Clone code as data; never run hooks or installers."""

import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def canonical_url(value):
    url = urlsplit(value)
    match = re.fullmatch(r"/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?/?", url.path)
    if (
        url.scheme != "https"
        or url.netloc != "github.com"
        or url.query
        or url.fragment
        or not match
    ):
        raise ValueError(
            "仅支持 https://github.com/owner/repo 公共仓库地址，不接受令牌、SSH或任意网址"
        )
    if any(s in {".", ".."} for s in match.groups()):
        raise ValueError("无效的 GitHub 仓库地址")
    return f"https://github.com/{match[1]}/{match[2]}"


def metadata(url):
    owner, name = canonical_url(url).split("/")[-2:]
    with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
        response = client.get(
            f"https://api.github.com/repos/{owner}/{name}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code != 200:
            raise ValueError(f"无法读取公共仓库元数据（HTTP {response.status_code}）；尚未下载源码")
        data = response.json()
    return {
        "url": canonical_url(url),
        "size_mb": round(data.get("size", 0) / 1024, 2),
        "default_branch": data.get("default_branch"),
        "archived": bool(data.get("archived")),
        "basis": "GitHub 元数据 size；包含历史影响，不是本次浅克隆精确大小",
    }


def tree_size(directory):
    total, count = 0, 0
    for folder, dirs, files in __import__("os").walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink():
                total += path.stat().st_size
                count += 1
            if total > 512 * 1024**2 or count > 25000:
                raise ValueError(
                    "仓库超过本次导入上限（512 MB或25,000文件），已停止；部分数据保留供核查"
                )
    return total, count


def clone(url, ref, destination):
    import os

    if destination.exists():
        raise ValueError("导入目录已存在；不覆盖或自动重放克隆")
    command = [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "filter.lfs.required=false",
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--no-recurse-submodules",
    ]
    if ref:
        command += ["--branch", ref]
    command += ["--", canonical_url(url) + ".git", str(destination)]
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    deadline = time.monotonic() + 90
    try:
        while process.poll() is None:
            if destination.exists():
                tree_size(destination)
            if time.monotonic() > deadline:
                raise ValueError("仓库下载超时，已停止；检查网络或选择更小仓库")
            time.sleep(0.2)
        if process.returncode:
            raise ValueError("公共仓库克隆失败；未运行仓库中的安装或启动脚本")
        size, count = tree_size(destination)
        commit = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return {
            "commit": commit,
            "bytes": size,
            "files": count,
            "clone": "depth=1; LFS/submodules excluded",
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
