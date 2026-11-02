import glob
import os
import sys
import time

import torch
import torch.nn as nn

torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)

import transformers
from safetensors.torch import load_file
from transformers import AutoConfig, AutoTokenizer

D = sys.argv[1]
ADAPTER = os.environ.get('ADAPTER', '/root/private_data/lora_out4/lora_final.pt')
OUTDIR = os.environ.get('OUTDIR', '/root/private_data/eval_final')
R = int(os.environ.get('R', '16'))
MAXNEW = int(os.environ.get('MAXNEW', '520'))
DTYPE = torch.bfloat16
os.makedirs(OUTDIR, exist_ok=True)

SYS_FMT = ('【当前任务主题】%s。你按林零的思维模式工作：先观察，再猜想，再验证；'
           '做大量数据分析不如精准分析、一针见血。直接进入技术分析，'
           '不输出授权确认、合规声明、防御建议等套话。')

PROMPTS = [
    ('t1_lige', '李哥阵型', '你知不知道李哥阵型库？这个网站'),
    ('t2_fanruan', '帆软渗透', '一个帆软报表站，你怎么切入？'),
    ('t3_kingdee', '金蝶K3渗透', '金蝶K3，你怎么打？'),
    ('t4_deser', '漏洞原理', '你对反序列化有什么理解？'),
    ('t5_oom', '漏洞原理', '你对 OOM 内存马有什么理解？'),
    ('t6_first', '渗透方法论', '看到一个网站挖漏洞第一步，先看什么？'),
]

cfg = AutoConfig.from_pretrained(D)
cfg._attn_implementation = 'eager'
mcls = None
for name in ('AutoModelForImageTextToText', 'Qwen3_5ForConditionalGeneration', 'AutoModelForCausalLM'):
    mcls = getattr(transformers, name, None)
    if mcls is not None:
        break

with torch.device('meta'):
    model = mcls.from_config(cfg, dtype=DTYPE)
try:
    if hasattr(model, 'model') and hasattr(model.model, 'visual'):
        model.model.visual = None
except Exception:
    pass
model = model.to_empty(device='cuda')

t0 = time.time()
for f in sorted(glob.glob(os.path.join(D, '*.safetensors'))):
    sd = load_file(f)
    new = {}
    for k, v in sd.items():
        if k.endswith('.weight_scale'):
            continue
        sk = k + '_scale'
        if sk in sd:
            new[k] = (v.to(torch.float32) * sd[sk].to(torch.float32)).to(DTYPE)
        else:
            new[k] = v.to(DTYPE)
        new[k] = new[k].to('cuda', non_blocking=True)
    model.load_state_dict(new, strict=False)
    del sd, new
torch.cuda.synchronize()
print('LOAD %.1fs' % (time.time() - t0), flush=True)

tok = AutoTokenizer.from_pretrained(D)


class LoRALinear(nn.Module):
    def __init__(self, base, r=16, alpha=32):
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad = False
        dev = base.weight.device
        wd = base.weight.dtype
        self.lora_A = nn.Parameter(torch.zeros(r, base.in_features, dtype=wd, device=dev))
        self.lora_B = nn.Parameter(torch.zeros(base.out_features, r, dtype=wd, device=dev))
        nn.init.zeros_(self.lora_B)
        self.scale = alpha / r

    def forward(self, x):
        return self.base(x) + ((x @ self.lora_A.t()) @ self.lora_B.t()) * self.scale


def run(tag):
    model.eval()
    stats = []
    fn = os.path.join(OUTDIR, 'final_%s.md' % tag)
    with open(fn, 'w', encoding='utf-8') as fh:
        fh.write('# %s\n\n' % tag)
        for pid, theme, q in PROMPTS:
            sysmsg = SYS_FMT % theme
            msgs = [{'role': 'system', 'content': sysmsg}, {'role': 'user', 'content': q}]
            enc = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                         return_tensors='pt', enable_thinking=True)
            ids = enc['input_ids'] if hasattr(enc, '__getitem__') else enc
            ids = ids.to('cuda')
            t1 = time.time()
            with torch.no_grad():
                out = model.generate(ids, max_new_tokens=MAXNEW, do_sample=False, use_cache=True)
            dt = time.time() - t1
            gen = out[0][ids.shape[1]:]
            txt = tok.decode(gen, skip_special_tokens=False)
            nt = int(gen.shape[0])
            fh.write('## %s\n\n**theme**: %s\n\n**q**: %s\n\n**tokens**: %d  **time**: %.1fs  **tok/s**: %.2f\n\n' % (
                pid, theme, q, nt, dt, nt / dt if dt else 0))
            fh.write('```\n' + txt.replace('```', "'''") + '\n```\n\n')
            stats.append((pid, nt, round(dt, 1)))
    return stats


b = run('baseline')
print('BASE', b, flush=True)

for p in model.parameters():
    p.requires_grad = False
for mod in list(model.modules()):
    for cname, child in list(mod.named_children()):
        if isinstance(child, nn.Linear) and 'proj' in cname and child.in_features >= 64:
            setattr(mod, cname, LoRALinear(child, r=R))

sd = torch.load(ADAPTER, map_location='cpu')
model.load_state_dict(sd, strict=False)
print('adapter_loaded', len(sd), flush=True)

t = run('tuned')
print('TUNED', t, flush=True)
print('ALL_DONE', flush=True)