---
name: repair-admitted-engine
description: Pilot repo-expert 的推理交付工作约定。
---

仅修改本任务许可的 adapter.py 或已准备源码配方下的 sglang/srt Python 文件。先读文件与 SHA，再编辑、检查；不改权重、测试、可信启动器或宿主文件。主控冻结候选、申请制品审批并在 Docker 中构建、运行和验收。报告改动、证据与局限，不把代码修改当作推理通过。

节点资产、栈配方和远端执行日志可用于只读诊断。远端engine_source还没有经准入的加载配方，不能把本机源码修复直接迁成远端任意内核修改。缺对应构建/数值测试证据时回报具体边界，仍由主控协调Owner资源登记、审批和验收。
