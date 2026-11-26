---
name: deploy-model-application
description: Prepare the approved model and deploy a source-tested application using task-owned resources only.
---
Read current status. Before candidate exists call prepare_model, then report the observed model identity. Existing 8B uses the registered GPU interactive worker; new ModelScope small models use task-local CPU inference. After candidate exists deploy and inspect health. Application executes in its own constrained container. Do not edit source or modify host drivers, shared jobs or unrelated resources. An uncertain previous launch cannot be blindly retried: inspect recorded evidence and report the blocker.
