"""Locked vLLM 0.8.5 SharedStorageConnector verification, same-node 1P1D only.

This upstream debug connector proves disk-backed KV reuse, not network/RDMA PD
deployment. The parent captures independent cache-hit and token-match evidence.
"""

SEED = "# The PD verification implementation is trusted and read-only under /checks.\n"

CHECK = '''import hashlib,json,os,subprocess,sys,time
from pathlib import Path
from importlib.metadata import version
assert version('vllm')=='0.8.5','Only locked vLLM 0.8.5 is admitted'
visible=os.environ['CUDA_VISIBLE_DEVICES'].split(',')
assert len(visible)==2 and len(set(visible))==2
stage_source="""import json,os,sys
from pathlib import Path
from transformers import AutoTokenizer
from vllm import LLM,SamplingParams
from vllm.config import KVTransferConfig
stage=sys.argv[1]
config={}
if stage!='baseline':config['kv_transfer_config']=KVTransferConfig(kv_connector='SharedStorageConnector',kv_role='kv_producer' if stage=='prefill' else 'kv_consumer',kv_connector_extra_config={'shared_storage_path':'/workspace/kv'})
tokenizer=AutoTokenizer.from_pretrained('/model',local_files_only=True)
prompt=tokenizer.encode('Explain inference latency and throughput in a server. '*12,add_special_tokens=True)
first=[]
if stage=='decode':first=json.loads(Path('/workspace/prefill.json').read_text())['tokens'];prompt+=first
llm=LLM(model='/model',tokenizer='/model',dtype='float16',trust_remote_code=False,max_model_len=512,max_num_seqs=1,gpu_memory_utilization=.08,enforce_eager=True,enable_prefix_caching=False,**config)
generated=llm.generate([{'prompt_token_ids':prompt}],SamplingParams(temperature=0,max_tokens=1 if stage=='prefill' else 15 if stage=='decode' else 16,ignore_eos=True))
tokens=list(generated[0].outputs[0].token_ids)
Path('/workspace/'+stage+'.json').write_text(json.dumps({'tokens':tokens,'prompt_tokens':len(prompt)}))
"""
Path('/workspace/kv').mkdir()
import textwrap
Path('/workspace/stage.py').write_text('def main():\\n'+textwrap.indent(stage_source,' ')+"\\nif __name__=='__main__':main()\\n")
details=[]
for stage,gpu in [('baseline',visible[1]),('prefill',visible[0]),('decode',visible[1])]:
 env={**os.environ,'CUDA_VISIBLE_DEVICES':gpu,'VLLM_USE_V1':'1','VLLM_LOGGING_LEVEL':'INFO','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'1'}
 start=time.perf_counter()
 p=subprocess.run([sys.executable,'-B','/workspace/stage.py',stage],env=env,capture_output=True,text=True,timeout=150)
 Path('/workspace/'+stage+'.log').write_text(p.stdout+p.stderr)
 if p.returncode:raise RuntimeError(stage+' failed: '+(p.stdout+p.stderr)[-4000:])
 details.append({'stage':stage,'elapsed_s':time.perf_counter()-start,'exit_code':p.returncode})
baseline=json.loads(Path('/workspace/baseline.json').read_text())['tokens']
split=json.loads(Path('/workspace/prefill.json').read_text())['tokens']+json.loads(Path('/workspace/decode.json').read_text())['tokens']
log=Path('/workspace/decode.log').read_text()
files=[p for p in Path('/workspace/kv').rglob('*.safetensors')]
hit='External Cache Hit!' in log and 'Inject KV cache of' in log
assert files and hit,'No independent upstream KV store/load evidence'
assert baseline==split and len(split)==16,'PD tokens differ from intact baseline'
print(json.dumps({'protocol':'pilot-pd-shared-storage-v1','passed':True,'scope':'same_node_sequential_1P1D_debug_connector','connector':'SharedStorageConnector','vllm_version':version('vllm'),'tokens_match':True,'output_tokens':len(split),'kv_files':len(files),'kv_bytes':sum(p.stat().st_size for p in files),'external_cache_hit':hit,'stages':details,'online_service':False,'cross_node':False,'network_kv':False}))
'''
