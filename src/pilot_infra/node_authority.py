"""One operation policy for assets, containers, software and node tests."""


def allowed_operations(policy):
    if policy.get("operations") is not None:
        return frozenset(policy["operations"])
    # Legacy permissions retain old behavior; new network/profile permissions
    # are never granted just by upgrading code or reading an old record.
    legacy = {"assets", "downloads", "containers", "inference"}
    if policy.get("allow_source_edit", False):
        legacy.add("software")
    return frozenset(legacy)


def require_operation(server, operation, *, source_edit=False):
    if not server.get("enabled") or server["policy"]["mode"] != "review":
        raise ValueError("服务器仅允许观察或已停用")
    if operation not in allowed_operations(server["policy"]):
        raise ValueError("所有者未允许此节点业务：" + operation)
    if source_edit and not server["policy"]["allow_source_edit"]:
        raise ValueError("所有者未允许此节点源码编辑")
