"""The publication guard detects private paths and credential patterns."""

from scripts.check_publication import check_blob


def test_private_paths_and_key_patterns_are_rejected():
    assert check_blob(".local/resources/vault.key", b"fixture")
    assert check_blob("workspace/output.txt", b"fixture")
    assert check_blob("src/module.py", b"-----BEGIN " + b"PRIVATE KEY-----")
    assert check_blob("config.txt", b"sk-" + b"A" * 32)
    assert check_blob("config.txt", b"api_key=" + b"'unguessable-credential-value'")
    assert check_blob("config.txt", b"https://" + b"name:password@real-host.invalid-test/path")


def test_public_templates_and_explicit_example_urls_are_allowed():
    assert not check_blob(".env.example", b"API_KEY=\n")
    assert not check_blob("tests/example.py", b"https://user:password@example.org/path")
    assert not check_blob("harness/runtime/plugins/workspace/index.mjs", b"export {}")
