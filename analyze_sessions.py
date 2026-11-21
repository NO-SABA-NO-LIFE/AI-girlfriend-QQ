import json
from datetime import datetime
from collections import defaultdict
lines=[json.loads(l) for l in open('logs/reply-events.jsonl') if l.strip()]
today=[e for e in lines if str(e.get('time','')).startswith('2026-11-20') and e.get('group_id')]
def pt(s): return datetime.strptime(s,'%Y-%m-%d %H:%M:%S')
by=defaultdict(list)
for e in today: by[(e['group_id'],e['user_id'])].append(e)
for key in sorted(by):
    evs=sorted(by[key], key=lambda e:e['time'])
    t0=pt(evs[0]['time']); t1=pt(evs[-1]['time'])
    gaps=[]
    for i in range(1,len(evs)):
        d=(pt(evs[i]['time'])-pt(evs[i-1]['time'])).total_seconds()
        gaps.append(int(d))
    print(key, 'n=%d'%len(evs), 'span=%ds'%(t1-t0).total_seconds(), 'gaps=',gaps)
