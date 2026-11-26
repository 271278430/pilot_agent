---
name: repair-python-issue
description: 定位并修复已登记Python问题，保留失败与回归证据。
---

Use only the role's registered tools. This skill does not change permissions.
1. Run the fixed target test to reproduce the issue.
2. Search definitions/references and read only relevant source ranges.
3. Make the smallest source edit; never edit tests/configuration or weaken assertions.
4. Run target and regression tests. Preserve remaining failures and excluded hypotheses with evidence.
5. Stop on an unknown side effect or changed baseline. Hand over the source patch and test evidence for independent acceptance.

Capabilities: repo.search, repo.read, repo.edit, repo.test, repo.evidence.
