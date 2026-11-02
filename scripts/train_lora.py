import glob
import math
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
DATA = os.environ.get('DATA', '/root/private_data/train2.jsonl')
SEQ = int(os.environ.get('SEQ', '2048'))
EPOCHS = float(os.environ.get('EPOCHS', '1'))
LR = float(os.environ.get('LR', '1e-4'))
R = int(os.environ.get('R', '16'))
MAX_STEPS = int(os.environ.get('MAX_STEPS', '0'))
ACC = int(os.environ.get('ACC', '1'))
OUT = os.environ.get('OUT', '/root/private_data/lora_out')
DTYPE = torch.bfloat16

print('cfg seq', SEQ, 'epochs', EPOCHS, 'lr', LR, 'r', R, 'acc', ACC, flush=True)

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
print('LOAD %.1fs vram %.2fGB' % (time.time() - t0, torch.cuda.memory_allocated() / 1024**3), flush=True)


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
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        nn.init.zeros_(self.lora_B)
        self.scale = alpha / r

    def forward(self, x):
        return self.base(x) + ((x @ self.lora_A.t()) @ self.lora_B.t()) * self.scale


for p in model.parameters():
    p.requires_grad = False

n_lora = 0
for mod in list(model.modules()):
    for cname, child in list(mod.named_children()):
        if isinstance(child, nn.Linear) and 'proj' in cname and child.in_features >= 64:
            setattr(mod, cname, LoRALinear(child, r=R))
            n_lora += 1
trainable = [p for p in model.parameters() if p.requires_grad]
print('lora_layers', n_lora, 'trainable_M', round(sum(p.numel() for p in trainable) / 1e6, 2), flush=True)

model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
if hasattr(model, 'enable_input_require_grads'):
    model.enable_input_require_grads()
model.train()

tok = AutoTokenizer.from_pretrained(D)

import json as _json

rows = []
skipped = 0
for line in open(DATA, encoding='utf-8'):
    r = _json.loads(line)
    msgs = r['messages']
    full = tok.apply_chat_template(msgs, tokenize=True, return_dict=True)
    ids = full['input_ids']
    if len(ids) < 32:
        skipped += 1
        continue
    pr = tok.apply_chat_template(msgs[:-1], add_generation_prompt=True, tokenize=True, return_dict=True)
    p = len(pr['input_ids'])
    lab = list(ids)
    for i in range(min(p, len(lab))):
        lab[i] = -100
    if len(ids) > SEQ:
        ids = ids[:SEQ]
        lab = lab[:SEQ]
    if all(x == -100 for x in lab):
        skipped += 1
        continue
    rows.append((ids, lab))
print('samples', len(rows), 'skipped', skipped, flush=True)

opt = torch.optim.AdamW(trainable, lr=LR)
total_steps = int(math.ceil(len(rows) / ACC * EPOCHS))
if MAX_STEPS:
    total_steps = min(total_steps, MAX_STEPS)
print('total_steps', total_steps, flush=True)

os.makedirs(OUT, exist_ok=True)
step = 0
t_start = time.time()
opt.zero_grad(set_to_none=True)
for ep in range(int(math.ceil(EPOCHS))):
    for i, (ids, lab) in enumerate(rows):
        if step >= total_steps:
            break
        t0 = time.time()
        x = torch.tensor([ids], device='cuda')
        y = torch.tensor([lab], device='cuda')
        out = model(input_ids=x, labels=y)
        loss = out.loss / ACC
        loss.backward()
        if (i + 1) % ACC == 0:
            opt.step()
            opt.zero_grad(set_to_none=True)
        step += 1
        dt = time.time() - t0
        el = time.time() - t_start
        if step % 5 == 0 or step == 1:
            print('step %d/%d ep%d loss %.4f %.1fs %.0ftok/s elapsed %.0fm peak %.1fGB' % (
                step, total_steps, ep, float(out.loss), dt, len(ids) / dt, el / 60,
                torch.cuda.max_memory_allocated() / 1024**3), flush=True)
        if step % 50 == 0:
            sd = {k: v.detach().float().cpu() for k, v in model.named_parameters() if 'lora_' in k}
            torch.save(sd, os.path.join(OUT, 'lora_%d.pt' % step))
            print('saved ckpt step', step, flush=True)
    if step >= total_steps:
        break

sd = {k: v.detach().float().cpu() for k, v in model.named_parameters() if 'lora_' in k}
torch.save(sd, os.path.join(OUT, 'lora_final.pt'))
print('DONE steps', step, 'saved', os.path.join(OUT, 'lora_final.pt'), flush=True)