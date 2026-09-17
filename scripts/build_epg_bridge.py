#!/usr/bin/env python3
import gzip, json, re, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; DOCS=ROOT/'docs'; WORK=ROOT/'.work'
SRC=ROOT/'epg_sources.json'; CFG=ROOT/'supplemental_sources.json'
ALL=DOCS/'all-sources.m3u'; FP=DOCS/'epg-fingerprints.json'
BRIDGE=WORK/'epg-bridge.json'; REPORT=DOCS/'epg-bridge-report.json'
ATTR=re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"'); CALL=re.compile(r'\b([KW][A-Z]{2,4})(?:-TV|-DT)?\b',re.I)
UA='twentypack20-iptv-epg-bridge/1.0'

def clean(v): return re.sub(r'\s+',' ',str(v or '').strip())
def key(v): return clean(v).casefold()
def norm(v):
    v=key(v).replace('&',' and '); v=re.sub(r'\([^)]*\)|\[[^]]*\]',' ',v)
    v=re.sub(r'\b(?:uhd|fhd|hd|sd|4k|1080p|720p|fast)\b',' ',v); v=re.sub(r'[^a-z0-9]+',' ',v)
    return re.sub(r'\s+',' ',v).strip()
def callsign(*vals):
    m=CALL.search(' '.join(clean(v) for v in vals if v)); return m.group(1).upper() if m else ''
def fetch(url,timeout=180):
    req=urllib.request.Request(url,headers={'User-Agent':UA}); return urllib.request.urlopen(req,timeout=timeout)
def bytes_(url):
    with fetch(url) as r: return r.read()
def parse_time(v):
    v=clean(v)
    for fmt,n in [('%Y%m%d%H%M%S %z',20),('%Y%m%d%H%M %z',18),('%Y%m%d%H%M%S',14),('%Y%m%d%H%M',12)]:
        try:
            d=datetime.strptime(v[:n],fmt); return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except: pass
    return None
def in_window(e,a,b):
    s=parse_time(e.attrib.get('start','')); t=parse_time(e.attrib.get('stop',''))
    return s is None or ((t is None or t>=a) and s<=b)
def playlist(path):
    out=[]; cur=None
    for line in path.read_text(encoding='utf-8').replace('\r','').split('\n'):
        line=line.strip()
        if line.startswith('#EXTINF:'):
            at={k:v for k,v in ATTR.findall(line)}; name=line.split(',',1)[1].strip() if ',' in line else at.get('tvg-name',''); cur={'attrs':at,'name':clean(name),'url':''}
        elif cur is not None and line and not line.startswith('#'):
            cur['url']=line; out.append(cur); cur=None
    return out
def src(e): return clean(e['attrs'].get('x-source') or 'unknown')
def rawid(e):
    a=e['attrs']; return clean(a.get('x-original-tvg-id') or a.get('tvg-id') or a.get('channel-id') or '')
def ekey(e): return f"{src(e)}|{rawid(e) or norm(e.get('name')) or e.get('url') or 'unknown'}"
def local(e,cfg):
    a=e['attrs']; text=' '.join([e.get('name',''),a.get('tvg-name',''),a.get('x-original-group',''),a.get('group-title',''),a.get('x-broadcast-area',''),rawid(e)])
    if key(a.get('x-source-kind'))=='local' or callsign(text): return True
    return any(key(w) in key(text) for w in cfg.get('smart_selection',{}).get('local_keywords',[]))
def mapping(data,tag):
    out={}; root=ET.fromstring(data); prefix=f'{tag}#'
    for n in root.findall('channel'):
        sid=clean(n.attrib.get('site_id')); rid=sid[len(prefix):] if sid.startswith(prefix) else sid
        if not rid: continue
        xid=clean(n.attrib.get('xmltv_id')); out[rid]={'id':xid or f'epgshare:{tag}:{rid}','name':clean(n.text),'xmltv_id':xid}
    return out
def pdict(e):
    p={'start':clean(e.attrib.get('start')),'stop':clean(e.attrib.get('stop')),'title':clean(e.findtext('title'))}
    for k,t in [('sub_title','sub-title'),('desc','desc'),('category','category'),('episode_num','episode-num')]:
        v=clean(e.findtext(t));
        if v: p[k]=v
    return p
def load_source(s,a,b):
    sid=clean(s.get('id')); tag=clean(s.get('tag')); mp=mapping(bytes_(s['mapping_url']),tag); raw_names={}; progs=defaultdict(list)
    r=fetch(s['url']); stream=gzip.GzipFile(fileobj=r) if s['url'].casefold().endswith('.gz') else r
    try:
        for _,e in ET.iterparse(stream,events=('end',)):
            if e.tag=='channel':
                rid=clean(e.attrib.get('id')); dn=e.find('display-name');
                if dn is not None and clean(dn.text): raw_names[rid]=clean(dn.text)
            elif e.tag=='programme':
                rid=clean(e.attrib.get('channel'))
                if in_window(e,a,b): progs[(mp.get(rid) or {'id':f'epgshare:{tag}:{rid}'})['id']].append(pdict(e))
            e.clear()
    finally:
        try: stream.close()
        except: pass
        try: r.close()
        except: pass
    ch={}
    for rid in set(raw_names)|set(mp):
        m=mp.get(rid,{}); cid=m.get('id') or f'epgshare:{tag}:{rid}'; name=m.get('name') or raw_names.get(rid) or rid
        q=ch.setdefault(cid,{'canonical_id':cid,'names':[],'callsigns':[],'providers':[],'programmes':[]})
        for n in (name,raw_names.get(rid,'')):
            if clean(n) and clean(n) not in q['names']: q['names'].append(clean(n))
        cs=callsign(name,rid)
        if cs and cs not in q['callsigns']: q['callsigns'].append(cs)
        q['providers'].append({'source':sid,'source_channel_id':rid,'priority':int(s.get('priority',0)),'kind':clean(s.get('kind'))})
        q['programmes'].extend(progs.get(cid,[]))
    return ch,{'id':sid,'name':clean(s.get('name')),'channels':len(ch),'programmes':sum(len(x) for x in progs.values()),'status':'ok','error':''}
def merge(dst,x):
    for k in ('names','callsigns','providers'):
        for v in x.get(k,[]):
            if v not in dst[k]: dst[k].append(v)
    seen={(p.get('start'),p.get('stop'),p.get('title')) for p in dst['programmes']}
    for p in x.get('programmes',[]):
        z=(p.get('start'),p.get('stop'),p.get('title'))
        if z not in seen: dst['programmes'].append(p); seen.add(z)
def indexes(ch):
    bn=defaultdict(set); bc=defaultdict(set)
    for cid,c in ch.items():
        for n in c['names']:
            if norm(n): bn[norm(n)].add(cid)
        for cs in c['callsigns']: bc[cs].add(cid)
    return {'canonical_ids':sorted(ch),'by_name':{k:sorted(v) for k,v in bn.items()},'by_callsign':{k:sorted(v) for k,v in bc.items()}}
def identify(e,ix,cfg,allow_name=True):
    rid=rawid(e); ids=set(ix['canonical_ids'])
    if rid and rid in ids: return rid,'exact-canonical-id'
    cs=callsign(e.get('name'),e['attrs'].get('tvg-name'),rid); m=ix['by_callsign'].get(cs,[]) if cs else []
    if len(m)==1: return m[0],'unique-callsign'
    if not allow_name or local(e,cfg): return '',''
    for v in (e.get('name'),e['attrs'].get('tvg-name')):
        m=ix['by_name'].get(norm(v),[]) if norm(v) else []
        if len(m)==1: return m[0],'unique-nonlocal-name'
    return '',''

def main():
    sc=json.loads(SRC.read_text()); cfg=json.loads(CFG.read_text())
    if not ALL.exists() or not FP.exists(): raise SystemExit('Build all-sources.m3u and epg-fingerprints.json first')
    now=datetime.now(timezone.utc); a=now-timedelta(hours=int(sc.get('history_hours',24))); b=now+timedelta(days=int(sc.get('future_days',7)))
    ch={}; reports=[]
    for s in sc.get('fallback_sources',[]):
        try:
            found,rep=load_source(s,a,b)
            for cid,x in found.items(): merge(ch.setdefault(cid,{'canonical_id':cid,'names':[],'callsigns':[],'providers':[],'programmes':[]}),x)
            reports.append(rep)
        except Exception as ex: reports.append({'id':clean(s.get('id')),'name':clean(s.get('name')),'channels':0,'programmes':0,'status':'fetch_failed','error':str(ex)})
    for c in ch.values(): c['programmes'].sort(key=lambda p:(p.get('start',''),p.get('title','')))
    ix=indexes(ch); entries=playlist(ALL); assignments={}; methods=defaultdict(int); fp=json.loads(FP.read_text()); allow=bool(sc.get('allow_unique_name_for_non_local',True))
    for e in entries:
        cid,method=identify(e,ix,cfg,allow)
        if not cid: continue
        assignments[ekey(e)]={'canonical_id':cid,'method':method}; methods[method]+=1
        s,r=src(e),rawid(e); c=ch.get(cid,{})
        if not s or not r or not c.get('programmes'): continue
        sr=fp.setdefault('sources',{}).setdefault(s,{'epg_url':'','status':'external_bridge','wanted_channels':0,'matched_channels':0,'channels':{},'error':''})
        cm=sr.setdefault('channels',{}); n=cm.get(r,{})
        n['external_identity']=cid; n['external_method']=method
        if not n.get('titles'):
            ps=c['programmes'][:24]; n.update({'name':(c.get('names') or [e.get('name','')])[0],'titles':[key(p.get('title')) for p in ps if key(p.get('title'))],'starts':[p.get('start','') for p in ps if p.get('title')]})
        cm[r]=n
    FP.write_text(json.dumps(fp,indent=2)); WORK.mkdir(exist_ok=True)
    bridge={'generated':now.isoformat(),'window_start':a.isoformat(),'window_end':b.isoformat(),'channels':ch,'indexes':ix,'assignments':assignments,'sources':reports}
    BRIDGE.write_text(json.dumps(bridge,separators=(',',':')))
    report={'generated':now.isoformat(),'window_start':a.isoformat(),'window_end':b.isoformat(),'external_channels':len(ch),'external_programmes':sum(len(x['programmes']) for x in ch.values()),'playlist_assignments':len(assignments),'assignment_methods':dict(sorted(methods.items())),'sources':reports}
    REPORT.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
if __name__=='__main__': main()
