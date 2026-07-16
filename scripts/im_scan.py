#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, csv, itertools, json, socket, string, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
WHOIS_HOST='whois.nic.im'; SOURCE_URL='https://raw.githubusercontent.com/johnkavin123/domains/main/6275.txt'; OUT=Path('results')

def observed():
 r=urlopen(Request(SOURCE_URL,headers={'User-Agent':'im-domain-audit/1.0'}),timeout=60); raw=r.read().decode(errors='replace'); return {x.strip().lower() for x in raw.splitlines() if x.strip()},r.headers.get('ETag')
def once(d):
 with socket.create_connection((WHOIS_HOST,43),timeout=20) as s:
  s.settimeout(20); s.sendall((d+'\r\n').encode()); b=[]
  while True:
   try:c=s.recv(8192)
   except socket.timeout:break
   if not c:break
   b.append(c)
 return b''.join(b).decode(errors='replace')
def check(d):
 err=''
 for n in range(1,5):
  try:
   t=once(d); l=t.lower(); st='available' if 'was not found' in l else ('registered' if 'domain name:' in l else 'unknown')
   return {'domain':d,'status':st,'attempts':n,'response_excerpt':' '.join(t.split())[:280]}
  except Exception as e: err=f'{type(e).__name__}: {e}'; time.sleep(n*2)
 return {'domain':d,'status':'error','attempts':4,'response_excerpt':err[:280]}
def main():
 OUT.mkdir(exist_ok=True); start=datetime.now(timezone.utc); obs,etag=observed(); alln=[''.join(x)+'.im' for x in itertools.product(string.ascii_lowercase,repeat=3)]; cand=[d for d in alln if d not in obs]
 rows=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:
  for i,r in enumerate(p.map(check,cand),1): rows.append(r); print(f'{i}/{len(cand)}',flush=True) if i%100==0 else None
 avail=[r['domain'] for r in rows if r['status']=='available']; re=[]; verified=[]
 for d in avail:
  r=check(d); re.append(r)
  if r['status']=='available': verified.append(d)
  time.sleep(.2)
 for name,data in [('whois_first_pass.csv',rows),('whois_recheck.csv',re)]:
  with (OUT/name).open('w',newline='',encoding='utf-8') as f:
   w=csv.DictWriter(f,fieldnames=['domain','status','attempts','response_excerpt']); w.writeheader(); w.writerows(data)
 (OUT/'available_3letter_im.txt').write_text('\n'.join(verified)+('\n' if verified else ''),encoding='utf-8')
 end=datetime.now(timezone.utc); unresolved=[r for r in rows if r['status'] not in ('available','registered')]
 summary={'started_at_utc':start.isoformat(),'finished_at_utc':end.isoformat(),'official_whois':WHOIS_HOST,'not_found_marker':'was not found','source_snapshot':SOURCE_URL,'source_etag':etag,'total_all_letter_combinations':len(alln),'observed_total_records':len(obs),'three_letter_names_in_snapshot':sum(d in obs for d in alln),'official_whois_candidates_checked':len(cand),'first_pass_available':len(avail),'twice_verified_available':len(verified),'unresolved_count':len(unresolved),'coa_im_result':next((r for r in re if r['domain']=='coa.im'),next((r for r in rows if r['domain']=='coa.im'),{'domain':'coa.im','status':'present_in_snapshot'}))}
 (OUT/'scan_summary.json').write_text(json.dumps(summary,indent=2)+'\n'); (OUT/'unresolved.json').write_text(json.dumps(unresolved,indent=2)+'\n'); print(json.dumps(summary,indent=2))
if __name__=='__main__': main()
