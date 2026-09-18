#!/usr/bin/env python3
import gzip, io, json, re, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; DOCS=ROOT/'docs'; WORK=ROOT/'.work'
CFG=ROOT/'supplemental_sources.json'; ECFG=ROOT/'epg_sources.json'; BRIDGE=WORK/'epg-bridge.json'; SELECT=DOCS/'selection-report.json'
ATTR=re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"'); UA='twentypack20-iptv-epg-builder/2.0'

def clean(v): return re.sub(r'\s+',' ',str(v or '').strip())
def key(v): return clean(v).casefold()
def norm(v):
    v=key(v).replace('&',' and '); v=re.sub(r'\([^)]*\)|\[[^]]*\]',' ',v); v=re.sub(r'\b(?:uhd|fhd|hd|sd|4k|1080p|720p|fast)\b',' ',v); v=re.sub(r'[^a-z0-9]+',' ',v)
    return re.sub(r'\s+',' ',v).strip()
def playlist(path):
    out=[]; cur=None
    for line in path.read_text(encoding='utf-8').replace('\r','').split('\n'):
        line=line.strip()
        if line.startswith('#EXTINF:'):
            a={k:v for k,v in ATTR.findall(line)}; n=line.rsplit(',',1)[1].strip() if ',' in line else a.get('tvg-name',''); cur={'attrs':a,'name':clean(n)}
        elif cur is not None and line and not line.startswith('#'):
            cur['url']=line; out.append(cur); cur=None
    return out
def source(e): return clean(e['attrs'].get('x-source'))
def rawid(e):
    a=e['attrs']; return clean(a.get('x-original-tvg-id') or a.get('channel-id'))
def bkey(e): return f"{source(e)}|{rawid(e) or norm(e.get('name')) or e.get('url') or 'unknown'}"
def fetch(url,timeout=180):
    q=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(q,timeout=timeout) as r: return r.read()
def gunzip(data): return gzip.decompress(data) if data[:2]==b'\x1f\x8b' else data
def ptime(v):
    v=clean(v)
    for fmt,n in [('%Y%m%d%H%M%S %z',20),('%Y%m%d%H%M %z',18),('%Y%m%d%H%M%S',14),('%Y%m%d%H%M',12)]:
        try:
            d=datetime.strptime(v[:n],fmt); return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except: pass
    return None
def within(p,a,b):
    s=ptime(p.get('start','')); t=ptime(p.get('stop','')); return s is None or ((t is None or t>=a) and s<=b)
def pdict(e):
    p={'start':clean(e.attrib.get('start')),'stop':clean(e.attrib.get('stop')),'title':clean(e.findtext('title'))}
    for k,t in [('sub_title','sub-title'),('desc','desc'),('category','category'),('episode_num','episode-num')]:
        v=clean(e.findtext(t));
        if v: p[k]=v
    i=e.find('icon')
    if i is not None and clean(i.attrib.get('src')): p['icon']=clean(i.attrib.get('src'))
    return p
def native(data,wanted,a,b):
    wanted=set(wanted); meta={}; progs=defaultdict(list); stream=io.BytesIO(gunzip(data))
    for _,e in ET.iterparse(stream,events=('end',)):
        if e.tag=='channel':
            cid=clean(e.attrib.get('id'))
            if cid in wanted:
                i=e.find('icon'); meta[cid]={'name':clean(e.findtext('display-name')),'icon':clean(i.attrib.get('src')) if i is not None else ''}
        elif e.tag=='programme':
            cid=clean(e.attrib.get('channel'))
            if cid in wanted:
                p=pdict(e)
                if within(p,a,b): progs[cid].append(p)
        e.clear()
    return meta,progs
def placeholder(e):
    a=e['attrs']; return {'name':clean(a.get('tvg-name') or e.get('name') or a.get('tvg-id')),'icon':clean(a.get('tvg-logo'))}
def dedupe(ps):
    out=[]; seen=set()
    for p in sorted(ps,key=lambda x:(x.get('start',''),x.get('stop',''),x.get('title',''))):
        z=(p.get('start',''),p.get('stop',''),key(p.get('title')))
        if z not in seen: seen.add(z); out.append(p)
    return out
def score(ps,typ):
    if not ps: return -1
    starts=[ptime(p.get('start')) for p in ps]; stops=[ptime(p.get('stop')) for p in ps]; starts=[x for x in starts if x]; stops=[x for x in stops if x]
    hours=max(0,(max(stops or starts)-min(starts)).total_seconds()/3600) if starts else 0
    rich=sum(int(bool(p.get('desc')))+int(bool(p.get('category')))+int(bool(p.get('episode_num'))) for p in ps)/max(1,len(ps)*3)
    return round(min(len(ps),2000)+min(hours,168)*2+rich*50+(5 if typ=='native' else 0),3)
def channel(cid,m):
    e=ET.Element('channel',{'id':cid}); ET.SubElement(e,'display-name').text=clean(m.get('name') or cid)
    if clean(m.get('icon')): ET.SubElement(e,'icon',{'src':clean(m['icon'])})
    return e
def programme(cid,p):
    a={'channel':cid};
    if p.get('start'): a['start']=p['start']
    if p.get('stop'): a['stop']=p['stop']
    e=ET.Element('programme',a); ET.SubElement(e,'title').text=clean(p.get('title') or 'Unknown')
    for k,t in [('sub_title','sub-title'),('desc','desc'),('category','category'),('episode_num','episode-num')]:
        if clean(p.get(k)): ET.SubElement(e,t).text=clean(p[k])
    if clean(p.get('icon')): ET.SubElement(e,'icon',{'src':clean(p['icon'])})
    return e

def main():
    cfg=json.loads(CFG.read_text()); ecfg=json.loads(ECFG.read_text()); ipath=DOCS/cfg.get('combined_output','index.m3u')
    if not ipath.exists(): raise SystemExit('Missing index.m3u')
    entries=playlist(ipath); now=datetime.now(timezone.utc); a=now-timedelta(hours=int(ecfg.get('history_hours',24))); b=now+timedelta(days=int(ecfg.get('future_days',7)))
    configs={clean(s.get('id')):s for s in cfg.get('sources',[]) if s.get('enabled',True)}
    wanted=defaultdict(set); outputs=defaultdict(set); byout={}; selected=set(); cand=defaultdict(list)
    for e in entries:
        oid=clean(e['attrs'].get('tvg-id'))
        if not oid: continue
        byout[oid]=e; s=source(e); r=rawid(e)
        if s and r: wanted[s].add(r); outputs[(s,r)].add(oid); selected.add((s,r,oid))
    if SELECT.exists():
        try:
            rep=json.loads(SELECT.read_text())
            for g in rep.get('collapsed_groups',[]):
                chosen=g.get('selected') or []
                if isinstance(chosen,dict): chosen=[chosen]
                candidates=list(chosen)+(g.get('alternatives') or [])
                target_oids=set()
                for q in chosen:
                    ss,sr=clean(q.get('source')),clean(q.get('tvg_id'))
                    if ss and sr: target_oids.update(outputs.get((ss,sr),set()))
                if not target_oids: continue
                for x in candidates:
                    xs,xr=clean(x.get('source')),clean(x.get('tvg_id'))
                    if xs and xr:
                        wanted[xs].add(xr)
                        outputs[(xs,xr)].update(target_oids)
        except Exception: pass
    native_reports=[]
    for sid,ids in sorted(wanted.items()):
        c=configs.get(sid,{}); url=clean(c.get('epg_url')); rep={'source':sid,'epg_url':url,'status':'no_epg' if not url else 'ok','wanted_channels':len(ids),'matched_channels':0,'programmes':0,'error':''}
        if not url: native_reports.append(rep); continue
        try:
            meta,pmap=native(fetch(url),ids,a,b)
            for rid in ids:
                ps=dedupe(pmap.get(rid,[])); oids=outputs.get((sid,rid),set())
                if not ps or not oids: continue
                rep['matched_channels']+=1; rep['programmes']+=len(ps)
                for oid in oids:
                    e=byout.get(oid)
                    if not e: continue
                    cand[oid].append({'type':'native','source':sid,'method':'exact-provider-id' if (sid,rid,oid) in selected else 'confirmed-equivalent-provider','meta':meta.get(rid) or placeholder(e),'programmes':ps})
        except Exception as ex: rep['status']='fetch_failed'; rep['error']=str(ex)
        native_reports.append(rep)
    bridge={'assignments':{},'channels':{}}
    if BRIDGE.exists():
        try: bridge=json.loads(BRIDGE.read_text())
        except: pass
    methods=defaultdict(int); bc=0
    for oid,e in byout.items():
        ass=(bridge.get('assignments') or {}).get(bkey(e))
        if not ass: continue
        cid=clean(ass.get('canonical_id')); ch=(bridge.get('channels') or {}).get(cid,{})
        ps=dedupe([p for p in ch.get('programmes',[]) if within(p,a,b)])
        if not ps: continue
        m=placeholder(e); names=ch.get('names') or []
        if names: m['name']=names[0]
        sources=','.join(sorted({clean(x.get('source')) for x in ch.get('providers',[]) if clean(x.get('source'))}))
        cand[oid].append({'type':'external','source':sources,'method':clean(ass.get('method')),'meta':m,'programmes':ps}); methods[clean(ass.get('method'))]+=1; bc+=1
    channels={}; programs=[]; counts=defaultdict(int); details=[]; covered=0
    for oid,e in byout.items():
        avail=cand.get(oid,[])
        if not avail: channels[oid]=channel(oid,placeholder(e)); continue
        ranked=sorted([(score(x['programmes'],x['type']),x) for x in avail],key=lambda x:x[0],reverse=True); sc,ch=ranked[0]
        counts[f"{ch['type']}:{ch['source'] or 'unknown'}"]+=1; covered+=1; channels[oid]=channel(oid,ch['meta']); programs.extend(programme(oid,p) for p in ch['programmes'])
        details.append({'tvg_id':oid,'channel':clean(e.get('name')),'selected_type':ch['type'],'selected_source':ch['source'],'match_method':ch['method'],'score':sc,'programmes':len(ch['programmes']),'candidate_count':len(avail)})
    root=ET.Element('tv',{'generator-info-name':'twentypack20 IPTV','generator-info-url':clean(cfg.get('site_base_url'))})
    for cid in sorted(channels,key=str.casefold): root.append(channels[cid])
    programs.sort(key=lambda e:(e.attrib.get('channel',''),e.attrib.get('start','')))
    for p in programs: root.append(p)
    out=DOCS/cfg.get('epg_output','epg.xml'); tree=ET.ElementTree(root); ET.indent(tree,space='  '); tree.write(out,encoding='utf-8',xml_declaration=True)
    with gzip.open(str(out)+'.gz','wb',compresslevel=9) as h: h.write(out.read_bytes())
    report={'generated':now.isoformat(),'window_start':a.isoformat(),'window_end':b.isoformat(),'configured_history_hours':int(ecfg.get('history_hours',24)),'configured_future_days':int(ecfg.get('future_days',7)),'playlist_channels':len(byout),'epg_channels':len(channels),'channels_with_programmes':covered,'channels_without_programmes':len(byout)-covered,'programme_coverage_percent':round(100*covered/max(1,len(byout)),2),'programmes':len(programs),'selection_counts':dict(sorted(counts.items())),'bridge_candidates':bc,'bridge_match_methods':dict(sorted(methods.items())),'native_sources':native_reports,'selection_details':details}
    (DOCS/'epg-report.json').write_text(json.dumps(report,indent=2)); print(json.dumps({k:v for k,v in report.items() if k!='selection_details'},indent=2))
if __name__=='__main__': main()
