---
name: build-model-environment
description: Build the approved CUDA training and inference recipe using bounded tools.
---
1. Read status and follow probe, plan, provision, download, cuda_smoke, nccl_smoke, inference, train, serve_benchmark.
2. advance starts a journaled job. inspect waits for that exact job. Do not poll status in place of inspect.
3. Loaded model artifacts must match the pinned revision and SHA; trust_remote_code is false.
4. A failed job requires diagnose. repair is allowed only when the returned diagnosis names that repair.
5. Occupied GPUs and driver incompatibilities require stopping. Do not kill other processes or change drivers.
6. Success requires every stage receipt. The training is an environment smoke test, not proof of teaching quality.
Capabilities: ml.status, ml.advance, ml.inspect, ml.diagnose, ml.repair.
