---
name: coordinate-model-application
description: Coordinate a user-defined model application with independent development, deployment and genuine-inference acceptance.
---
Read the approved goal and current evidence. Delegate InfraPilot to prepare the model before source exists; then RepoPilot builds and tests the requested application. Hand its immutable candidate to InfraPilot for deployment. Call verify yourself. Failed HTTP protocol or app behavior goes back to RepoPilot with evidence; model startup and container evidence goes to InfraPilot. Never replace a failed requirement with a canned demo. Finish only after the CURRENT candidate passes real-model acceptance. Tool permissions and budgets come from the backend, never this skill.

当前开放应用使用官方原生 Subagent：infra_agent 准备模型，repo_agent 编写并测试候选，send_message(agent_id=返回ID) 继续同一 InfraPilot 部署候选。按依赖顺序执行；子报告不等于子停止，先等官方finished通知。list_agents发现既有子角色；恢复后复用ID，不调用旧delegate_repo/delegate_infra。最终verify与finish由Supervisor独立完成，子消息不是验收凭据。
