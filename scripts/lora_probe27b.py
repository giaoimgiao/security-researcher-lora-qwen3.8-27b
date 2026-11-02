import glob
import math
import os
import sys
import time
import traceback

import torch
import torch.nn as nn

torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)

import transformers
from safetensors.torch import load_file
from transformers import AutoConfig

D = sys.argv[1]
SEQ = int(os.environ.get('SEQ', '256'))
STEPS = int(os.environ.get('STEPS', '2'))
R = int(os.environ.get('R', '8'))
DTYPE = torch.bfloat16

print('tf', transformers.__version__, 'SEQ', SEQ, 'R', R, flush=True)

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

files = sorted(glob.glob(os.path.join(D, '*.safetensors')))
t0 = time.time()
for f in files:
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
print('LOAD %.1fs vram %.2fGB' % (time.time() - t0, torch.cuda.memory_allocated() / 1024**3), flush=True)


class LoRALinear(nn.Module):
    def __init__(self, base, r=8, alpha=16):
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad = False
        inf, outf = base.in_features, base.out_features
        dev = base.weight.device
        wd = base.weight.dtype
        self.lora_A = nn.Parameter(torch.zeros(r, inf, dtype=wd, device=dev))
        self.lora_B = nn.Parameter(torch.zeros(outf, r, dtype=wd, device=dev))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        nn.init.zeros_(self.lora_B)
        self.scale = alpha / r

    def forward(self, x):
        out = self.base(x)
        return out + ((x @ self.lora_A.t()) @ self.lora_B.t()) * self.scale


# FREEZE_ALL: LoRA ��训参数不能带上 lm_head / embed_tokens
for _p in model.parameters():
    _p.requires_grad = False

n_lora = 0
for name, mod in list(model.named_modules()):
    for cname, child in list(mod.named_children()):
        if isinstance(child, nn.Linear) and 'proj' in cname:
            setattr(mod, cname, LoRALinear(child, r=R))
            n_lora += 1
trainable = [p for p in model.parameters() if p.requires_grad]
print('lora_layers', n_lora, 'trainable_M', round(sum(p.numel() for p in trainable) / 1e6, 2), flush=True)
print('vram_after_lora %.2fGB' % (torch.cuda.memory_allocated() / 1024**3), flush=True)

try:
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    if hasattr(model, 'enable_input_require_grads'):
        model.enable_input_require_grads()
    print('grad_ckpt enabled', flush=True)
except Exception as e:
    print('grad_ckpt fail', str(e)[:100], flush=True)

model.train()
opt = torch.optim.AdamW(trainable, lr=1e-4)
V = cfg.text_config.vocab_size if hasattr(cfg, 'text_config') else cfg.vocab_size

for step in range(STEPS):
    t0 = time.time()
    ids = torch.randint(100, V - 100, (1, SEQ), device='cuda')
    out = model(input_ids=ids, labels=ids)
    loss = out.loss
    loss.backward()
    opt.step()
    opt.zero_grad(set_to_none=True)
    torch.cuda.synchronize()
    dt = time.time() - t0
    print('STEP %d  %.1fs  loss %.4f  tok/s %.2f  peak_GB %.2f' % (
        step, dt, float(loss), SEQ / dt, torch.cuda.max_memory_allocated() / 1024**3), flush=True)

print('PROBE_DONE', flush=True)