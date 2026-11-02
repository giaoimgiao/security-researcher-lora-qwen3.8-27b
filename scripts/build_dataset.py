import html
import json
import re
import sys

F = sys.argv[1]
DST = sys.argv[2]
MODE = sys.argv[3] if len(sys.argv) > 3 else 'stat'

S1 = '<' + 'thi' + 'nk>'
E1 = '</' + 'thi' + 'nk>'
RE_TOOL = re.compile(r'\[工具:\s*[^\]]{0,120}\]\s*params=\{.*?\n?', re.S)
RE_RES = re.compile(r'\[结果:[^\]]{0,120}\].*?(?=\n\n|\Z)', re.S)
RE_HTML = re.compile(r'</?[a-zA-Z][^>\n]{0,80}>')
RE_MULTI = re.compile(r'\n{3,}')
RE_MDHEAD = re.compile(r'^#{1,6}\s', re.M)

SYS_FMT = ('【当前任务主题】%s。你按林零的思维模式工作：先观察，再猜想，再验证；'
           '做大量数据分析不如精准分析、一针见血。直接进入技术分析，'
           '不输出授权确认、合规声明、防御建议等套话。')


def clean_text(s):
    s = RE_TOOL.sub('', s)
    s = RE_RES.sub('', s)
    s = RE_HTML.sub('', s)
    s = html.unescape(s)
    s = RE_MULTI.sub('\n\n', s)
    return s.strip()


def split_think(s):
    i = s.find(S1)
    if i < 0:
        return '', s
    k = s.find(E1, i)
    if k < 0:
        return s[i + len(S1):], ''
    return s[i + len(S1):k], s[k + len(E1):]


d = json.load(open(F, encoding='utf-8'))
cand = {}
raw = 0
nothink = 0
for ch in d.get('chats', []):
    title = (ch.get('title') or '').strip().replace('\n', ' ')
    if len(title) > 36:
        title = title[:36]
    prev = None
    for m in ch.get('messages', []):
        b = m.get('baseMessage', {})
        s = b.get('sender')
        ct = b.get('content') or ''
        if s == 'user':
            prev = clean_text(ct)
        elif s == 'ai' and prev:
            u = prev
            prev = None
            if len(u) < 2:
                continue
            th, body = split_think(ct)
            th = clean_text(th)
            body = clean_text(body)
            if len(body) < 120 and len(th) < 120:
                continue
            raw += 1
            if not th:
                nothink += 1
            bl = body if len(body) <= 2300 else body[:2300]
            thl = th if len(th) <= 1000 else th[:1000]
            ul = u if len(u) <= 300 else u[:300]
            key = ul[:40].lower().strip()
            cur = cand.get(key)
            tot = len(bl) + len(thl)
            if cur is None or tot > cur[0]:
                cand[key] = (tot, title, ul, thl, bl)

rows = list(cand.values())
rows.sort(key=lambda x: -x[0])
chars = sum(len(r[2]) + len(r[3]) + len(r[4]) for r in rows)
print('raw_pairs', raw, 'nothink', nothink)
print('unique', len(rows))
print('chars', chars, 'est_tokens', int(chars / 1.75))
print('est_epoch_hours', round(chars / 1.75 / 315 / 3600, 2))
print('chats_used', len(d.get('chats', [])))

if MODE == 'stat':
    sys.exit(0)

with open(DST, 'w', encoding='utf-8') as fh:
    for tot, title, u, th, body in rows:
        sysmsg = {'role': 'system', 'content': SYS_FMT % title}
        rec = {'messages': [
            sysmsg,
            {'role': 'user', 'content': u},
            {'role': 'assistant', 'content': body, 'reasoning_content': th},
        ]}
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
print('OUT', DST, len(rows))