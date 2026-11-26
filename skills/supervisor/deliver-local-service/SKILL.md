---
name: deliver-local-service
description: 按补丁验收、部署、服务验收的依赖交付固定任务。
---

Use only registered Supervisor tools. Child text is not acceptance evidence.
1. Inspect durable status; respect pause/cancel and existing results.
2. Delegate the registered Repo task. Require accepted baseline/patch fingerprint.
3. Delegate Infra only after Repo independent acceptance.
4. Run independent verification against the real service and source fingerprint.
5. Finish using the verification record. Preserve failures; do not replay unknown actions.

Capabilities: supervisor.status, supervisor.delegate_repo, supervisor.delegate_infra, supervisor.verify, supervisor.finish.
