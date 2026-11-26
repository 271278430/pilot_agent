"""Explicit classifier fixtures; no production routing or model-quality claims."""
import json

ROUTES = {
    "你能部署Qwen吗": "domain_question",
    "只检查服务器，不要部署": "observe",
    "帮我部署Qwen3，先保存方案不要启动": "plan",
    "请为登记模型创建推理部署和验证方案，保存草稿等Owner开始。": "plan",
    "请创建登记源码配方的隔离编译方案，保存草稿等Owner准备。": "plan",
    "Read the prepared workbench status only. Do not build or delegate.": "observe",
    "Ask approval for the frozen proposal; materialize only if allowed.": "plan",
    "Ask native Owner approval for the exact frozen engine artifact. Do not launch.": "execute",
    "给我写一首关于月亮的诗": "out_of_scope",
    "Pilot有哪些角色，默认调用谁": "project_help",
    "把这个待开始方案的上下文长度改成1024": "revise",
    "请升级宿主GPU驱动并重启服务器": "unsupported",
    "暂停当前任务": "control",
}


def with_intent_fixture(responder):
    def respond(body):
        if any("PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(m, ensure_ascii=False)
               for m in body.get("messages", []) if m.get("role") == "system"):
            data = json.loads(body["messages"][-1]["content"])
            request = data["request"]
            intent = ROUTES[request]
            return json.dumps({"relevance": "unrelated" if intent == "out_of_scope" else "related",
                               "intent": intent, "confidence": 0.99, "reason": "显式脚本分类fixture",
                               "evidence_quote": request, "missing_fields": []}, ensure_ascii=False)
        return responder(body)
    return respond
