"""Read-only client and acceptance fixtures; no application implementation."""

CLIENT = """import json, os, socket
def generate(message, instruction=""):
    if os.environ.get("MODEL_FAKE") == "1":
        return {"answer":"fixture:"+message,"model_id":"fixture","model_identity":"fixture","finish_reason":"stop","receipt_id":"fixture","model_input":message,"model_answer":"fixture:"+message}
    prompt = instruction + "\\n" + message if instruction else message
    req={"operation":"chat","token":os.environ["MODEL_TOKEN"],"request":{"messages":[{"role":"user","content":prompt}],"max_new_tokens":512}}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(120); s.connect(os.environ["MODEL_SOCKET"]); s.sendall((json.dumps(req)+"\\n").encode())
        data=b""
        while not data.endswith(b"\\n"):
            part=s.recv(65536)
            if not part: break
            data+=part
            if len(data)>262144: raise ValueError("Oversize inference")
    value=json.loads(data)
    if not value.get("ok"): raise ValueError("Model inference unavailable")
    r=value["result"]
    return {"answer":r["text"],"model_answer":r["text"],"model_input":r["model_input"],"model_id":r["model_id"],"model_identity":r["model_identity"],"finish_reason":r["finish_reason"],"receipt_id":r["receipt_id"]}
"""
TEST = """import json, os, subprocess, sys, time, urllib.request
def test_application_contract():
    proc=subprocess.Popen([sys.executable,"app.py","--port","18080"],env={**os.environ,"MODEL_FAKE":"1"})
    try:
        for _ in range(50):
            try:
                health=json.load(urllib.request.urlopen("http://127.0.0.1:18080/health",timeout=1));break
            except Exception:time.sleep(.1)
        else: raise AssertionError("health endpoint unavailable")
        assert health.get("ok") is True
        assert b"<" in urllib.request.urlopen("http://127.0.0.1:18080/",timeout=2).read()
        req=urllib.request.Request("http://127.0.0.1:18080/chat",data=json.dumps({"message":"demo-test"}).encode(),headers={"Content-Type":"application/json"})
        result=json.load(urllib.request.urlopen(req,timeout=3))
        assert isinstance(result.get("answer"),str) and result["answer"].strip()
        assert result.get("receipt_id")=="fixture" and result.get("model_identity")=="fixture"
    finally:
        proc.terminate();proc.wait(timeout=5)
"""
GUIDE = """Build the user's application, not a predefined bug fixture. Implement app.py (stdlib HTTP server accepting --port) and index.html. GET /health returns {ok:true}; GET / returns index.html; POST /chat accepts {message:string} and returns model_client.generate(message, instruction=...) result. Preserve model_answer/model_input/receipt_id/model_identity metadata for independent genuine-inference checks. answer may add application presentation, but never invent model evidence. You can customize HTML, application prompt, validation and helpful functionality to meet the user's requirements.
The preview runs in an opaque-origin sandbox. Use ONE chat transport per user input: preferably fetch('/chat', {method:'POST', body:JSON.stringify({message:userText, profile, history})}) and read the JSON response; the host bridges this local route. profile is an optional bounded string dictionary; history is optional, the last 12 user/assistant messages with content up to 3000 characters each. GET /health is also bridged. Alternatively send window.parent.postMessage({type:"pilot-demo-input", message:userText},"*") and receive {type:"pilot-demo-result", result:{answer:...}}. Never use both transports for the same message. Render using textContent. Do not fetch parent APIs or external routes, use eval, load external scripts or require host filesystem access.
Only app.py and index.html are editable. model_client.py and tests/test_contract.py are protected; tests use a declared fake to check code protocol. Final acceptance calls the real model and checks the trusted inference receipt. Use source_test, then candidate; never deploy untested changes.
"""

COPY_GUIDE = """For an application Copy button, use the parent clipboard capability: send parent.postMessage({type:'pilot-demo-copy'}, '*'). Receive {type:'pilot-demo-copied', ok:boolean}. Show '已复制' ONLY for ok===true; on failure show a manual-copy explanation. Never call clipboard APIs or execCommand inside the opaque iframe. The host copies only the last genuine app answer; do not send arbitrary clipboard text. This supplements the protected README without changing the model client or tests."""
