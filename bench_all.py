import base64, json, time
from pathlib import Path
import httpx

env={}
for line in open('.env'):
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); env[k]=v.strip().strip('"')
BASE=env['OPENAI_BASE_URL'].rstrip('/'); KEY=env.get('OPENAI_API_KEY','')
HEADERS={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'}
TIMEOUT=55.0; MAX_TOK=350
MODELS=['gemini-2.5-flash','gemini-2.5-flash-lite','gemini-3-flash','gemini-3-flash-preview',
'gemini-3.1-flash-lite','gemini-3.1-flash-lite-preview','gemini-3.1-pro','gemini-3.1-pro-preview',
'gemini-3.5-flash','gemini-3.6-flash','gemini-3.7-flash','gemini-3.8-flash','gemini-4-argon']
data_uri='data:image/png;base64,'+base64.b64encode(Path('bench_test.png').read_bytes()).decode()

def common_body(m):
    return {'model':m,'max_tokens':MAX_TOK,'reasoning_effort':'low','temperature':0.7}

def call(client, body):
    t0=time.perf_counter()
    r=client.post(BASE+'/chat/completions',headers=HEADERS,json=body)
    dt=time.perf_counter()-t0; r.raise_for_status()
    j=r.json(); ch=j['choices'][0]; msg=ch.get('message',{})
    content=msg.get('content')
    if isinstance(content,list):
        content=''.join(p.get('text','') for p in content if isinstance(p,dict))
    u=j.get('usage',{})
    return {'sec':round(dt,2),'ok':True,'finish':ch.get('finish_reason'),'ct':u.get('completion_tokens'),
            'pt':u.get('prompt_tokens'),'tt':u.get('total_tokens'),'reply':(content or '').strip()}

results={}
with httpx.Client(timeout=TIMEOUT) as client:
    for m in MODELS:
        results[m]={}
        b=common_body(m); b['messages']=[{'role':'system','content':'你是亚托莉，简洁回答。'},
            {'role':'user','content':'用一句话介绍你自己。'}]
        try:
            print('[TEXT]',m,flush=True); results[m]['text']=call(client,b)
            print('  ->',results[m]['text']['sec'],'s',results[m]['text']['finish'],flush=True)
        except Exception as e:
            results[m]['text']={'ok':False,'err':repr(e)[:200]}; print('  !! ERR',repr(e)[:150],flush=True)
        b=common_body(m); b['messages']=[{'role':'user','content':[
            {'type':'text','text':'请简要描述这张图里有哪些形状、颜色和文字。'},
            {'type':'image_url','image_url':{'url':data_uri}}]}]
        try:
            print('[VISION]',m,flush=True); results[m]['vision']=call(client,b)
            print('  ->',results[m]['vision']['sec'],'s',results[m]['vision']['finish'],flush=True)
        except Exception as e:
            results[m]['vision']={'ok':False,'err':repr(e)[:200]}; print('  !! ERR',repr(e)[:150],flush=True)

Path('bench_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print('WROTE bench_results.json',flush=True)
