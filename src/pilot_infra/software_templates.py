"""Reviewed seed and independent checks, separate from candidate source."""

HTTP_SEED = """def summarize(rows):
    raise NotImplementedError("Implement request counts, failures, p95 and token throughput")
"""
HTTP_CHECK = """import json,sys,importlib.util
sys.path.insert(0,'/workspace')
import client
cases=[[],[{'ok':True,'latency_ms':10,'output_tokens':4},{'ok':False,'latency_ms':30,'output_tokens':0}],
       [{'ok':True,'latency_ms':x,'output_tokens':8} for x in range(1,21)]]
for rows in cases:
 result=client.summarize(rows,elapsed_seconds=2.0)
 assert result['attempted']==len(rows)
 assert result['successful']==sum(r['ok'] for r in rows)
 assert result['failed']==sum(not r['ok'] for r in rows)
 assert result['output_tokens']==sum(r['output_tokens'] for r in rows if r['ok'])
 assert result['request_throughput_rps']==sum(r['ok'] for r in rows)/2.0
 assert result['output_throughput_tps']==sum(r['output_tokens'] for r in rows if r['ok'])/2.0
 assert result['failure_rate']==(sum(not r['ok'] for r in rows)/len(rows) if rows else None)
 assert result['latency_p95_ms']==(sorted(r['latency_ms'] for r in rows if r['ok'])[max(0,__import__('math').ceil(.95*sum(r['ok'] for r in rows))-1)] if any(r['ok'] for r in rows) else None)
print(json.dumps({'protocol':'pilot-http-metrics-contract-v1','cases':len(cases),'passed':True}))
"""
RMS_SEED = """import torch
def run(x,eps=1e-6):
    values=x.float()
    return (values*torch.rsqrt(values.square().mean(dim=-1,keepdim=True)+eps)).to(x.dtype)
"""
RMS_CHECK = """import json,sys,time,torch
sys.path.insert(0,'/workspace')
import kernel
torch.manual_seed(42);torch.set_num_threads(1)
assert torch.cuda.is_available()
cases=[]
for dtype in [torch.float16,torch.float32]:
 for rows,width in [(1,32),(17,137),(256,1024),(2048,4096)]:
  x=torch.randn(rows,width,device='cuda',dtype=dtype)
  ref=(x.double()*torch.rsqrt(x.double().square().mean(-1,keepdim=True)+1e-6)).to(dtype)
  out=kernel.run(x);torch.cuda.synchronize()
  assert out.shape==x.shape and out.dtype==dtype and torch.isfinite(out).all()
  error=(out-ref).abs().max().item()
  tolerance=.003 if dtype==torch.float16 else .00003
  assert error<=tolerance,(rows,width,dtype,error,tolerance)
  cases.append({'shape':[rows,width],'dtype':str(dtype),'max_abs_error':error,'tolerance':tolerance})
x=torch.randn(2048,4096,device='cuda',dtype=torch.float16)
def reference(x):
 v=x.float();return (v*torch.rsqrt(v.square().mean(-1,keepdim=True)+1e-6)).to(x.dtype)
def measure(fn):
 for _ in range(5):fn(x)
 torch.cuda.synchronize();samples=[]
 for _ in range(20):
  start,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
  start.record();fn(x);end.record();end.synchronize();samples.append(start.elapsed_time(end))
 return sorted(samples)[len(samples)//2]
baseline=measure(reference);candidate=measure(kernel.run)
print(json.dumps({'protocol':'pilot-rmsnorm-cuda-v1','correctness_passed':True,'cases':cases,
                 'timing_method':'cuda_events_median_20','baseline_ms':baseline,'candidate_ms':candidate,
                 'speedup':baseline/candidate,'device':torch.cuda.get_device_name(0)}))
"""
PROFILE_SEED = """def work():
    return sum(x*x for x in range(100000))
"""
PROFILE_CHECK = """import cProfile,io,json,pstats,sys
sys.path.insert(0,'/workspace');import target
profiler=cProfile.Profile();result=profiler.runcall(target.work)
assert result==sum(x*x for x in range(100000))
stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumulative').print_stats(10)
print(json.dumps({'protocol':'pilot-cpu-profile-v1','passed':True,'profile':stream.getvalue()}))
"""


def material(template, python):
    from .pd_template import CHECK, SEED
    specs = {
        "http_metrics": ("script", "cpu", {"client.py": HTTP_SEED}, {"verify.py": HTTP_CHECK}),
        "rmsnorm_cuda": ("operator", "cuda", {"kernel.py": RMS_SEED}, {"verify.py": RMS_CHECK}),
        "cpu_profile": (
            "profile",
            "cpu",
            {"target.py": PROFILE_SEED},
            {"verify.py": PROFILE_CHECK},
        ),
        "pd_vllm_shared": ("pd", "cuda", {"scope.py": SEED}, {"verify.py": CHECK}),
    }
    kind, platform, files, checks = specs[template]
    return {
        "kind": kind,
        "platform": platform,
        "files": files,
        "checks": checks,
        "steps": [
            {
                "stage": kind + "_validation",
                "argv": [python, "-B", "/checks/verify.py"],
                "timeout": 500 if kind == "pd" else 60,
            }
        ],
    }
