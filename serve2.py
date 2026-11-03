import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

D = sys.argv[1]
DTYPE = torch.bfloat16
MODEL_ID = os.environ.get('MODEL_ID', 'security-researcher-lora-qwen3.6-27b')
PORT = int(os.environ.get('PORT', '8000'))
THINK_END = '</think'

print('loading', D, flush=True)
t0 = time.time()
tok = AutoTokenizer.from_pretrained(D)
model = AutoModelForCausalLM.from_pretrained(D, torch_dtype=DTYPE, device_map='cuda')
model.eval()
print('LOAD %.1fs' % (time.time() - t0), flush=True)

_eos = set()
try:
    if isinstance(tok.eos_token_id, int):
        _eos.add(int(tok.eos_token_id))
except Exception:
    pass
for t in ('<|im_end|>', '<|endoftext|>'):
    try:
        i = tok.convert_tokens_to_ids(t)
        if isinstance(i, int) and i >= 0:
            _eos.add(int(i))
    except Exception:
        pass
EOS_IDS = tuple(sorted(_eos))
print('EOS_IDS', EOS_IDS, flush=True)

LOCK = threading.Lock()


def generate(ids, max_tokens, temp, on_text=None):
    out = []
    cur = ids
    past = None
    full = ''
    sent = 0
    for _ in range(max_tokens):
        with torch.no_grad():
            o = model(input_ids=cur, past_key_values=past, use_cache=True)
        past = o.past_key_values
        logits = o.logits[0, -1].float()
        if temp and temp > 0:
            p = torch.softmax(logits / temp, dim=-1)
            nxt = torch.multinomial(p, 1)
        else:
            nxt = logits.argmax().view(1)
        tid = int(nxt.view(-1)[0].item())
        if tid in EOS_IDS:
            break
        out.append(nxt.view(1))
        cur = nxt.view(1, 1)
        if on_text is not None:
            full = tok.decode(torch.cat(out), skip_special_tokens=True)
            if len(full) > sent:
                on_text(full[sent:])
                sent = len(full)
    if on_text is None:
        full = tok.decode(torch.cat(out), skip_special_tokens=True) if out else ''
    return full


def split_think(s):
    i = s.find(THINK_END)
    if i < 0:
        return '', s
    j = s.find('>', i)
    th = s[:i]
    ans = s[j + 1:] if j >= 0 else ''
    return th.strip(), ans.strip()


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

    def do_GET(self):
        if self.path.startswith('/health'):
            self._send(200, {'ok': True, 'model': MODEL_ID})
        elif self.path.startswith('/v1/models'):
            self._send(200, {'object': 'list', 'data': [
                {'id': MODEL_ID, 'object': 'model', 'created': 0, 'owned_by': 'local'}]})
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

        msgs = body.get('messages') or []
        mt = int(body.get('max_tokens') or 512)
        temp = float(body.get('temperature') or 0.0)
        stream = bool(body.get('stream'))
        tools = body.get('tools')
        et = body.get('enable_thinking')
        if et is None:
            ck = body.get('chat_template_kwargs') or {}
            if isinstance(ck, dict) and 'enable_thinking' in ck:
                et = ck.get('enable_thinking')
        if et is None:
            et = body.get('thinking')
        thinking = True if et is None else bool(et)

        kw = {'add_generation_prompt': True, 'return_tensors': 'pt',
              'enable_thinking': thinking}
        if tools:
            kw['tools'] = tools

        try:
            enc = tok.apply_chat_template(msgs, **kw)
            ids = enc['input_ids'] if hasattr(enc, 'keys') else enc
            if hasattr(ids, 'to'):
                ids = ids.to('cuda')
            else:
                ids = torch.tensor([ids], device='cuda')
        except Exception as e:
            self._send(500, {'error': 'template: ' + repr(e)})
            return

        if stream:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            cid = 'chatcmpl-%d' % int(time.time())

            def chunk(delta, finish=None):
                o = {'id': cid, 'object': 'chat.completion.chunk',
                     'created': int(time.time()), 'model': MODEL_ID,
                     'choices': [{'index': 0, 'delta': delta,
                                  'finish_reason': finish}]}
                try:
                    self.wfile.write(('data: ' + json.dumps(o, ensure_ascii=False) + '\n\n').encode())
                    self.wfile.flush()
                except Exception:
                    pass

            chunk({'role': 'assistant'})
            buf = {'t': ''}

            def on_text(t):
                buf['t'] += t
                chunk({'content': t})

            with LOCK:
                try:
                    generate(ids, mt, temp, on_text)
                except Exception as e:
                    chunk({'content': '\n[SERVER_ERR] ' + repr(e)})
            chunk({}, 'stop')
            try:
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception:
                pass
            return

        with LOCK:
            try:
                full = generate(ids, mt, temp, None)
            except Exception as e:
                torch.cuda.empty_cache()
                self._send(500, {'error': repr(e)})
                return
        th, ans = split_think(full)
        msg = {'role': 'assistant', 'content': ans, 'reasoning_content': th}
        self._send(200, {
            'id': 'chatcmpl-lora', 'object': 'chat.completion',
            'created': int(time.time()), 'model': MODEL_ID,
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': msg}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})


def main():
    s = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    print('SERVING port=%d model=%s' % (PORT, MODEL_ID), flush=True)
    s.serve_forever()


if __name__ == '__main__':
    main()
