---
name: coordinate-inference-delivery
description: Pilot infra-lead 的推理交付工作约定。
---

先理解相关性和动作，再读取当前资源与策略。默认主控执行；只有当前证据满足准入才委派 Infra 或 Repo 专家。所有推理部署使用登记 Docker 镜像，禁止宿主进程兜底。使用持久 Job、确切审批、独立验收与停止清理；不靠健康检查宣告业务完成。

新增节点模型/镜像先读取 asset_catalog 和固定模型元数据、支持资料，必要时用 asset_propose / stack_propose 提交登记提案供Owner审阅。登记不是执行；准备使用 asset_request_prepare 的原生一次审批，节点成功核验才进入部署选择。确认同节点模型/镜像和兼容硬约束，未知adapter拒绝GPU。断线查原操作，不重放unknown；到期租约、标签/真实ID清理和独立验收必须核对。开放配方不代表任意厂商已实测。
