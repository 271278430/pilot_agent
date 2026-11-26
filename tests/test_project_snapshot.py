import io
import stat
import zipfile

import httpx
import pytest

from pilot_projects.snapshot import download, extract_archive


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, text in entries:
            z.writestr(name, text)
    return buffer.getvalue()


@pytest.mark.parametrize("path", ["repo/../../outside", "/etc/passwd", "repo/C:/outside", "repo/a\\..\\b", "repo/.git/config"])
def test_snapshot_cannot_write_paths_outside_source(tmp_path, path):
    content = io.BytesIO(archive([(path, "unsafe")]))
    target = tmp_path / "source"
    with pytest.raises(ValueError):
        extract_archive(content, target)
    assert not target.exists()


def test_snapshot_rejects_symbolic_links_and_duplicate_sources(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        i = zipfile.ZipInfo("repo/link")
        i.create_system = 3
        i.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(i, "../../outside")
    with pytest.raises(ValueError, match="符号链接"):
        extract_archive(io.BytesIO(buffer.getvalue()), tmp_path / "source")
    target = tmp_path / "source"
    target.mkdir()
    with pytest.raises(ValueError, match="已存在"):
        extract_archive(io.BytesIO(archive([("repo/file.py", "safe")])), target)


def test_snapshot_pins_commit_and_removes_only_its_temporary_archive(tmp_path, monkeypatch):
    sha = "a" * 40
    payload = archive([("repo-" + sha + "/README.md", "Source README"), ("repo-" + sha + "/src/app.py", "print(1)")])
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "/commits/" in str(request.url):
            return httpx.Response(200, json={"sha": sha})
        return httpx.Response(200, content=payload)

    factory = httpx.Client
    monkeypatch.setattr("pilot_projects.snapshot.httpx.Client", lambda **kw: factory(transport=httpx.MockTransport(handler), **kw))
    unrelated = tmp_path / "other.zip"
    unrelated.write_text("keep")
    report = download("https://github.com/pypa/sampleproject", "main", tmp_path / "workspace")
    assert seen[-1] == "https://codeload.github.com/pypa/sampleproject/zip/" + sha
    assert report["commit"] == sha and not report["git_history"]
    assert (tmp_path / "workspace/src/app.py").read_text() == "print(1)"
    assert unrelated.read_text() == "keep" and not list(tmp_path.glob(".source-*.zip"))


def test_snapshot_size_and_file_count_limit_precede_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("pilot_projects.snapshot.MAX_BYTES", 2)
    with pytest.raises(ValueError, match="上限"):
        extract_archive(io.BytesIO(archive([("repo/app.py", "larger")])), tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()
