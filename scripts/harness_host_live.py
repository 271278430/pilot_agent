"""Finite real-model probe of the deployed official Web, with private login."""
import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/harness-host-20261118/live.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["start", "status", "check"])
    parser.add_argument("--port", type=int, default=18890)
    args = parser.parse_args()
    origin = f"http://127.0.0.1:{args.port}"
    entry = urlsplit(json.loads((ROOT / ".local/harness-web/entry.json").read_text())["url"])
    login = urlunsplit((entry.scheme, f"127.0.0.1:{args.port}", entry.path, entry.query, entry.fragment))
    with httpx.Client(follow_redirects=True, timeout=180) as client:
        response = client.get(login)
        assert response.status_code == 200 and "@pilot/delivery-ui" in response.text

        def rpc(endpoint, payload, channel="pilot-api"):
            response = client.post(origin + "/" + channel + "/" + endpoint, json={"type": "client-request",
                "rpcId": uuid.uuid4().hex, "method": endpoint, "payload": {"args": payload} if channel == "api" else payload})
            response.raise_for_status()
            result = response.json()["result"]
            if not result["ok"]:
                raise RuntimeError(result["error"])
            return result["value"]

        if args.mode == "check":
            catalog = rpc("catalog", {})
            print(json.dumps({"official_web": True, "delivery_module": True, "apps": len(catalog["applications"]), "legacy_visible": len(catalog["legacy_conversations"])}))
            return
        if args.mode == "start":
            from pilot_core.config import credentials
            from pilot_host.bridge import HostDomain

            model = credentials()["REPOPILOT_MODEL"]
            sid = "harness-live-" + uuid.uuid4().hex
            rpc("session/create", {"request": {"sessionId": sid, "cwd": str(ROOT / "runs/harness-inbox"), "agentPreset": "supervisor"}}, "api")
            plan = {"title": "Harness 迁移验收：中文问答", "goal": "创建一个调用已配置 API 的简洁中文问答网页，验证官方 Harness 角色与领域插件交付",
                "model_source": "configured_api", "model_id": model,
                "application_requirements": ["输入问题并显示真实模型答案", "预置两个示例问题", "清空输入与答案", "复制本次真实答案", "显示调用状态"],
                "acceptance_examples": ["用一句话解释二次函数是什么", "计算1+2，只输出结果"]}
            # Fixed finite QA plan; the actual code, delegation, model invocation
            # and acceptance are performed by real GLM and official agents.
            reviewed = HostDomain(ROOT).call("plan", sid, arguments={"plan": plan, "request_id": uuid.uuid4().hex})
            execution = rpc("start", {"session_id": sid})
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps({"planning_session": sid, "execution_session": execution["sessionId"],
                "application_id": reviewed["application"]["id"], "controller_model": model,
                "controller_kind": "real_model", "pricing": "not_priced", "qa_scope": "configured API, isolated CPU app container"}, indent=2, ensure_ascii=False))
        result = json.loads(REPORT.read_text())
        view = rpc("view", {"session_id": result["execution_session"]})
        app = view["application"]
        result.update(status=app["status"], candidate=app["state"].get("candidate"), verification=app["state"].get("verification"),
                      error=app["state"].get("error"), last_error=app["state"].get("last_error"))
        if app["status"] == "completed":
            html = rpc("html", {"session_id": result["execution_session"]})
            answer = rpc("preview", {"session_id": result["execution_session"], "binding": html["binding"], "message": "5加7等于多少，请只输出数字"})
            result["fresh_preview"] = answer
            (REPORT.parent / "preview.html").write_text(html["html"])
        REPORT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps({"application": result["application_id"], "status": app["status"],
            "candidate": (result.get("candidate") or {}).get("id"), "verification": (result.get("verification") or {}).get("passed"),
            "error": result["error"], "last_error": result["last_error"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
