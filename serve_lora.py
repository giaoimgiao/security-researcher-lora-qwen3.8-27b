import glob
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
import torch.nn as nn

torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_math_sdp(True)

import transformers
from safetensors.torch import load_file
from transformers import AutoConfig, AutoTokenizer

D = sys.argv[1]
ADAPTER = os.environ.get('ADAPTER', '/root/private_data/lora_out4/lora_final_fp16.pt')
PORT = int(os.environ.get('PORT', '8000'))
R = int(os.environ.get('R', '16'))
DTYPE = torch.bfloat16
MODEL_ID = 'security-researcher-lora-qwen3.6-27b'

EMPTY_ANCHOR = ('【当前任务主题】渗透方法论。你按林零的思维模式工作：先观察，再猜想，再验证；'
                '做大量数据分析不如精准分析、一针见血。直接进入技术分析，'
                '不输出授权确认、合规声明、防御建议等套话。')

cfg = AutoConfig.from_pretrained(D)
cfg._attn_implementation = 'sdpa'
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
# 不整体 to_empty：Linear 权重先留空，逐分片按 INT8 放置，避免吃满 54GB
model = model.to_empty(device='cuda')

class QLinear(nn.Module):
    """INT8 权重常驻显存，forward 时逐层反量化。"""

    def __init__(self, w_i8, scale, out_features, in_features, bias=None):
        super().__init__()
        self.w_i8 = nn.Parameter(w_i8, requires_grad=False)
        self.w_scale = nn.Parameter(scale, requires_grad=False)
        self.bias = bias
        self.in_features = in_features
        self.out_features = out_features

    def forward(self, x):
        w = self.w_i8.to(x.dtype) * self.w_scale.to(x.dtype)
        return torch.nn.functional.linear(x, w, self.bias)


def set_param(model, name, tensor):
    parent_name, _, attr = name.rpartition('.')
    parent = model.get_submodule(parent_name) if parent_name else model
    parent._parameters[attr] = nn.Parameter(tensor, requires_grad=False)
    return name


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
print('BASE_LOAD %.1fs' % (time.time() - t0), flush=True)


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
        self.scale = alpha / r

    def forward(self, x):
        return self.base(x) + ((x @ self.lora_A.t()) @ self.lora_B.t()) * self.scale


class QLinear(nn.Module):
    """INT8 权重常驻显存，forward 时逐层反量化，显存占用减半。"""

    def __init__(self, w_i8, scale, out_features, in_features, bias=None):
        super().__init__()
        self.w_i8 = nn.Parameter(w_i8, requires_grad=False)
        self.w_scale = nn.Parameter(scale, requires_grad=False)
        self.bias = bias
        self.in_features = in_features
        self.out_features = out_features

    def forward(self, x):
        w = self.w_i8.to(x.dtype) * self.w_scale.to(x.dtype)
        return torch.nn.functional.linear(x, w, self.bias)


def set_param(model, name, tensor):
    parent_name, _, attr = name.rpartition('.')
    parent = model.get_submodule(parent_name) if parent_name else model
    old = parent._parameters.get(attr)
    if old is not None:
        parent._parameters[attr] = nn.Parameter(tensor, requires_grad=False)
    else:
        setattr(parent, attr, nn.Parameter(tensor, requires_grad=False))
    return name


for p in model.parameters():
    p.requires_grad = False
n_layer = 0
for mod in list(model.modules()):
    for cname, child in list(mod.named_children()):
        if isinstance(child, (nn.Linear, QLinear)) and 'proj' in cname and child.in_features >= 64:
            setattr(mod, cname, LoRALinear(child, r=R))
            n_layer += 1

sd = torch.load(ADAPTER, map_location='cpu')
missing, unexpected = model.load_state_dict(sd, strict=False)
print('ADAPTER %s tensors=%d layers=%d missing=%d unexpected=%d' % (
    ADAPTER, len(sd), n_layer, len(missing), len(unexpected)), flush=True)

tok = AutoTokenizer.from_pretrained(D)
model.eval()
LOCK = threading.Lock()

IM_END = '<|im_end|>'
EOT = '<|endoftext|>'


def clean(s):
    for t in (IM_END, EOT):
        s = s.replace(t, '')
    return s.strip()


_eos = [tok.eos_token_id,
        tok.convert_tokens_to_ids(LT + '|im_end|' + GT),
        tok.convert_tokens_to_ids(LT + '|endoftext|' + GT)]
EOS_IDS = tuple(sorted({int(x) for x in _eos if isinstance(x, int) and x >= 0}))
print('EOS_IDS', EOS_IDS, 'eos_token', tok.eos_token, flush=True)
PREFILL_CHUNK = int(os.environ.get('PREFILL_CHUNK', '512'))

# special markers are built from escapes on purpose
LT = '\x3c'
GT = '\x3e'
THINK_END = LT + '/think'
TOOL_OPEN = LT + 'tool_call' + GT
TOOL_CLOSE = LT + '/tool_call' + GT
TOOL_CALL_RE = re.compile(LT + 'tool_call' + GT + '(.*?)' + LT + '/tool_call' + GT, re.S)
FUNC_RE = re.compile(LT + 'function=([^' + GT + r'\s]+)' + GT + '(.*?)' + LT + '/function' + GT, re.S)
PARAM_RE = re.compile(LT + 'parameter=([^' + GT + r'\s]+)' + GT + '(.*?)' + LT + '/parameter' + GT, re.S)


def norm_content(c):
    if c is None:
        return ''
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict):
                if p.get('text') is not None:
                    parts.append(str(p.get('text')))
                elif p.get('content') is not None:
                    parts.append(str(p.get('content')))
            elif isinstance(p, str):
                parts.append(p)
        return '\n'.join(parts)
    return str(c)


def norm_messages(msgs):
    # Qwen template iterates tool_calls[].function.arguments as a mapping,
    # but OpenAI sends a JSON string -> TypeError without this conversion.
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        mm = {'role': m.get('role') or 'user',
              'content': norm_content(m.get('content'))}
        tcs = m.get('tool_calls')
        if isinstance(tcs, list) and tcs:
            norm = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get('function') or {}
                args = fn.get('arguments')
                if isinstance(args, str):
                    s = args.strip()
                    try:
                        args = json.loads(s) if s else {}
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                norm.append({'id': tc.get('id') or ('call_%d' % len(norm)),
                             'type': 'function',
                             'function': {'name': fn.get('name') or '',
                                          'arguments': args}})
            mm['tool_calls'] = norm
        if m.get('tool_call_id'):
            mm['tool_call_id'] = m['tool_call_id']
        out.append(mm)
    return out


def _mk_call(name, args):
    return {'id': 'call_%d%d' % (int(time.time() * 1000) % 100000, len(name)),
            'type': 'function',
            'function': {'name': name,
                         'arguments': json.dumps(args or {}, ensure_ascii=False)}}


def parse_tool_calls(text):
    calls = []
    for m in TOOL_CALL_RE.finditer(text):
        block = m.group(1).strip()
        if not block:
            continue
        if block.startswith('{'):
            try:
                obj = json.loads(block)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                fn = obj.get('function') if isinstance(obj.get('function'), dict) else {}
                name = obj.get('name') or fn.get('name')
                args = obj.get('arguments')
                if args is None:
                    args = fn.get('arguments')
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {'raw': args}
                if name:
                    calls.append(_mk_call(name, args))
            continue
        fm = FUNC_RE.search(block)
        if not fm:
            continue
        name = fm.group(1).strip()
        args = {}
        for pm in PARAM_RE.finditer(fm.group(2)):
            v = pm.group(2)
            if v.startswith('\n'):
                v = v[1:]
            args[pm.group(1).strip()] = v.strip()
        calls.append(_mk_call(name, args))
    return calls


def generate_chunked(input_ids, max_new_tokens, temp=0.0):
    """Chunked prefill + manual decode.

    DCU 版 torch 没有 memory-efficient attention 内核，整段 prefill 会物化
    N x N 注意力矩阵导致 OOM。这里把输入切块逐块前向，KV cache 累积，
    单块矩阵只有 chunk x chunk。
    """
    n = int(input_ids.shape[1])
    past = None
    logits = None
    with torch.no_grad():
        for i in range(0, n, PREFILL_CHUNK):
            c = input_ids[:, i:i + PREFILL_CHUNK]
            out = model(input_ids=c, past_key_values=past, use_cache=True)
            past = out.past_key_values
            logits = out.logits[:, -1, :]

    gen = []
    cur = logits
    for _ in range(max_new_tokens):
        if temp > 0:
            probs = torch.softmax(cur / temp, dim=-1)
            nxt = torch.multinomial(probs, num_samples=1)
        else:
            nxt = cur.argmax(dim=-1, keepdim=True)
        tid = int(nxt.item())
        if tid in EOS_IDS:
            break
        gen.append(nxt)
        if len(gen) >= max_new_tokens:
            break
        with torch.no_grad():
            out = model(input_ids=nxt, past_key_values=past, use_cache=True)
        past = out.past_key_values
        cur = out.logits[:, -1, :]

    if not gen:
        return torch.zeros((1, 0), dtype=torch.long, device=input_ids.device)
    return torch.cat(gen, dim=1)


def prefill(ids):
    n = int(ids.shape[1])
    past = None
    logits = None
    with torch.no_grad():
        for i in range(0, n, PREFILL_CHUNK):
            c = ids[:, i:i + PREFILL_CHUNK]
            out = model(input_ids=c, past_key_values=past, use_cache=True)
            past = out.past_key_values
            logits = out.logits[:, -1, :]
    return past, logits


def pick(cur, temp):
    if temp > 0:
        probs = torch.softmax(cur / temp, dim=-1)
        return torch.multinomial(probs, num_samples=1)
    return cur.argmax(dim=-1, keepdim=True)


def run_generation(ids, mt, temp, on_delta, thinking=True):
    past, cur = prefill(ids)
    gen = []
    sent_r = 0
    sent_c = 0
    full = ''
    for _ in range(mt):
        nxt = pick(cur, temp)
        if int(nxt.item()) in EOS_IDS:
            break
        gen.append(nxt)
        full = tok.decode(torch.cat(gen)[0], skip_special_tokens=False)
        if on_delta is not None:
            if not thinking:
                if len(full) > sent_c:
                    on_delta('content', full[sent_c:])
                    sent_c = len(full)
            else:
                i = full.find(THINK_END)
                if i >= 0:
                    gt = full.find(GT, i)
                    rtext = full[:i]
                    ctext = full[gt + 1:] if gt >= 0 else ''
                else:
                    rtext, ctext = full, ''
                if len(rtext) > sent_r:
                    on_delta('reasoning', rtext[sent_r:])
                    sent_r = len(rtext)
                if len(ctext) > sent_c:
                    on_delta('content', ctext[sent_c:])
                    sent_c = len(ctext)
        if TOOL_CLOSE in full:
            break
        with torch.no_grad():
            out = model(input_ids=nxt, past_key_values=past, use_cache=True)
        past = out.past_key_values
        cur = out.logits[:, -1, :]
    return full, len(gen)


def finalize(full):
    s = full
    i = s.find(THINK_END)
    if i >= 0:
        gt = s.find(GT, i)
        th = s[:i]
        ans = s[gt + 1:] if gt >= 0 else ''
    else:
        th, ans = '', s
    th = clean(th)
    ans = clean(ans)
    calls = parse_tool_calls(ans)
    if calls:
        ans = clean(TOOL_CALL_RE.sub('', ans))
    if not ans and not calls and th:
        ans, th = th, ''
    return th, ans, calls


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self, obj):
        self.wfile.write(('data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n').encode())
        self.wfile.flush()

    def _stream_chat(self, ids, mt, temp, thinking):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        cid = 'chatcmpl-%d' % (int(time.time() * 1000) % 100000)

        def chunk(delta, finish=None):
            self._sse({'id': cid, 'object': 'chat.completion.chunk',
                       'created': int(time.time()), 'model': MODEL_ID,
                       'choices': [{'index': 0, 'delta': delta,
                                    'finish_reason': finish}]})

        chunk({'role': 'assistant'})
        hold = {'buf': ''}

        def on_delta(kind, text):
            if kind == 'reasoning':
                chunk({'reasoning_content': text})
                return
            buf = hold['buf'] + text
            if TOOL_OPEN in buf:
                head = buf[:buf.index(TOOL_OPEN)]
                hold['buf'] = ''
                if head:
                    chunk({'content': head})
                return
            h = 0
            for k in range(min(len(TOOL_OPEN) - 1, len(buf)), 0, -1):
                if buf.endswith(TOOL_OPEN[:k]):
                    h = k
                    break
            if h:
                out = buf[:len(buf) - h]
                hold['buf'] = buf[len(buf) - h:]
            else:
                out = buf
                hold['buf'] = ''
            if out:
                chunk({'content': out})

        try:
            full, ntok = run_generation(ids, mt, temp, on_delta, thinking)
        except Exception as e:
            torch.cuda.empty_cache()
            chunk({'content': '\n[error] ' + repr(e)}, 'stop')
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        torch.cuda.empty_cache()

        th, ans, calls = finalize(full)
        if calls:
            hold['buf'] = ''
            chunk({'tool_calls': [{'index': i, 'id': c['id'], 'type': 'function',
                                   'function': c['function']}
                                  for i, c in enumerate(calls)]}, 'tool_calls')
        else:
            tail = hold['buf']
            if not ans and th:
                tail = (tail + th) if tail else th
            if tail:
                chunk({'content': tail})
            chunk({}, 'stop')
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

    def do_GET(self):
        if self.path.rstrip('/') == '/v1/models':
            self._send(200, {'object': 'list', 'data': [
                {'id': MODEL_ID, 'object': 'model', 'created': 0, 'owned_by': 'giaoimgiao'}]})
        elif self.path.startswith('/health'):
            self._send(200, {'ok': True, 'model': MODEL_ID})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self.path.startswith('/v1/chat/completions'):
            self._send(404, {'error': 'not found'})
            return
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n).decode())
        except Exception:
            self._send(400, {'error': 'bad json'})
            return

        msgs = norm_messages(body.get('messages') or [])
        if not msgs:
            self._send(400, {'error': 'messages required'})
            return

        tools = body.get('tools')
        if not isinstance(tools, list) or not tools:
            tools = None
        if tools is not None and body.get('tool_choice') == 'none':
            tools = None
        if tools is None and not any(m['role'] == 'system' for m in msgs):
            msgs = [{'role': 'system', 'content': EMPTY_ANCHOR}] + msgs

        mt = int(body.get('max_tokens') or 512)
        mt = max(16, min(mt, 8192))
        temp = float(body.get('temperature') or 0.0)
        et = body.get('enable_thinking')
        if et is None:
            ck = body.get('chat_template_kwargs')
            if isinstance(ck, dict):
                et = ck.get('enable_thinking')
        if et is None and body.get('thinking') is not None:
            et = body.get('thinking')
        thinking = False if et is None else bool(et)
        stream = bool(body.get('stream'))

        try:
            with open('/tmp/req.log', 'a') as fh:
                fh.write(json.dumps({'t': int(time.time()), 'stream': stream,
                                     'tools': len(tools) if tools else 0,
                                     'mt': mt,
                                     'roles': [m['role'] for m in msgs]},
                                    ensure_ascii=False) + '\n')
        except Exception:
            pass

        kw = {'add_generation_prompt': True, 'return_tensors': 'pt',
              'enable_thinking': thinking}
        if tools:
            kw['tools'] = tools

        with LOCK:
            t_start = time.time()
            try:
                enc = tok.apply_chat_template(msgs, **kw)
                ids = enc['input_ids'] if hasattr(enc, '__getitem__') else enc
                ids = ids.to('cuda')
            except Exception as e:
                torch.cuda.empty_cache()
                self._send(500, {'error': 'template: ' + repr(e)})
                return

            if stream:
                self._stream_chat(ids, mt, temp, thinking)
                return

            try:
                full, ntok = run_generation(ids, mt, temp, None)
            except Exception as e:
                torch.cuda.empty_cache()
                self._send(500, {'error': repr(e)})
                return
            torch.cuda.empty_cache()
        dt = time.time() - t_start
        th, ans, calls = finalize(full)
        msg = {'role': 'assistant', 'content': ans, 'reasoning_content': th}
        if calls:
            msg['tool_calls'] = calls

        self._send(200, {
            'id': 'chatcmpl-lora',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': MODEL_ID,
            'choices': [{'index': 0,
                         'finish_reason': 'tool_calls' if calls else 'stop',
                         'message': msg}],
            'usage': {'prompt_tokens': int(ids.shape[1]),
                      'completion_tokens': int(ntok),
                      'total_tokens': int(ids.shape[1] + ntok)},
            'elapsed_s': round(dt, 1),
        })


print('SERVING port=%d adapter=%s' % (PORT, ADAPTER), flush=True)
ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()