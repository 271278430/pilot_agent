---
name: recover-owned-service
description: 部署登记本机服务，只处理自身端口故障。
---

This skill does not authorize shell, host driver changes, package installation, or other users' processes.
1. Probe the registered runtime.
2. Stage only the independently accepted patch with matching baseline and SHA.
3. Start the owned localhost service and check its task identity.
4. If startup fails, diagnose using actual evidence. Only an owned-port conflict permits the bounded repair.
5. Start once after repair; stop on unknown faults or unsettled processes.

Capabilities: infra.probe, infra.stage, infra.start, infra.diagnose, infra.repair.
