import json,re,hashlib,os
from datetime import datetime,timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

ROOT=os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT,"config.json"),encoding="utf-8") as f: CFG=json.load(f)
DATA=os.path.join(ROOT,"data"); os.makedirs(DATA,exist_ok=True)
STATE=os.path.join(DATA,"state.json"); ALERTS=os.path.join(DATA,"last_alerts.json")
HEAD={"User-Agent":"Mozilla/5.0 F2CAR-Stock-Radar/1.0"}

def norm(s): return re.sub(r"\s+"," ",s or "").strip()
def number(pattern,s):
    m=re.search(pattern,s or "",re.I)
    return int(re.sub(r"\D","",m.group(1))) if m else None
def parse(text,url):
    text=norm(text)
    price=number(r"(\d[\d .]*)\s*€",text)
    kms=number(r"(\d[\d .]*)\s*km",text)
    ym=re.search(r"\b(0[1-9]|1[0-2])/(20\d{2})\b",text)
    year=ym.group(0) if ym else None
    fuel=next((x for x in ["Elétrico","Diesel","Gasolina","Híbrido Plug-In","Híbrido"] if x.lower() in text.lower()),None)
    if price is None or kms is None: return None
    title=re.split(r"\b(?:0[1-9]|1[0-2])/20\d{2}\b|\b\d[\d .]*\s*km\b|\b\d[\d .]*\s*€",text,1)[0].strip(" -|")
    if len(title)<4: return None
    v={"title":title[:180],"year":year,"km":kms,"fuel":fuel,"price":price,"url":url}
    raw="|".join(str(v.get(k) or "").lower() for k in ["title","year","fuel"])
    v["id"]=hashlib.sha1(raw.encode()).hexdigest()[:16]
    return v

def scrape(url):
    try:
        r=requests.get(url,headers=HEAD,timeout=CFG["timeout_seconds"])
        if not r.ok: return None
        soup=BeautifulSoup(r.text,"html.parser")
        out=[]; seen=set()
        for a in soup.find_all("a",href=True):
            href=urljoin(url,a["href"]); text=norm(a.parent.get_text(" ",strip=True) if a.parent else a.get_text(" ",strip=True))
            if href in seen or len(text)<30 or len(text)>1200: continue
            v=parse(text,href)
            if v and v["id"] not in {x["id"] for x in out}:
                out.append(v); seen.add(href)
        return out
    except Exception: return None

def load(path,default):
    try:
        with open(path,encoding="utf-8") as f:return json.load(f)
    except:return default
def save(path,obj):
    with open(path,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)

def main():
    old=load(STATE,{"initialized":False,"f2car":[],"suppliers":{},"pending_removals":{}})
    current={}; failures=[]; f2=[]
    for key,site in CFG["sites"].items():
        stock=scrape(site["url"])
        if stock is None:
            failures.append(site["name"]); continue
        if key=="f2car": f2=stock
        else: current[site["name"]]=stock
    alerts=[]; now=datetime.now(timezone.utc).isoformat()
    if old["initialized"]:
        f2map={x["id"]:x for x in f2}
        pending=old.get("pending_removals",{})
        for supplier,stock in current.items():
            smap={x["id"]:x for x in stock}
            oldstock=old.get("suppliers",{}).get(supplier,[])
            oldmap={x["id"]:x for x in oldstock}
            # New/changed supplier cars
            for v in stock:
                if v["id"] not in f2map:
                    alerts.append({"type":"new","supplier":supplier,"vehicle":v})
                elif v["id"] in f2map:
                    f=f2map[v["id"]]
                    changes={k:(f.get(k),v.get(k)) for k in ["price","km","year","fuel"] if f.get(k)!=v.get(k)}
                    if changes: alerts.append({"type":"change","supplier":supplier,"vehicle":v,"changes":changes})
            # Two consecutive successful absences
            newids=set(smap)
            for oid,v in oldmap.items():
                if oid not in newids:
                    key=supplier+"::"+oid
                    n=pending.get(key,0)+1
                    pending[key]=n
                    if n>=CFG["removal_confirmations"] and oid in f2map:
                        alerts.append({"type":"removed","supplier":supplier,"vehicle":v,"f2_url":f2map[oid]["url"]})
                        pending.pop(key,None)
                else:
                    pending.pop(supplier+"::"+oid,None)
        old["pending_removals"]=pending
    state={"initialized":True,"last_run":now,"f2car":f2,"suppliers":current,"pending_removals":old.get("pending_removals",{})}
    save(STATE,state); save(ALERTS,{"generated_at":now,"alerts":alerts,"failures":failures})
    print(json.dumps({"f2car":len(f2),"suppliers":{k:len(v) for k,v in current.items()},"alerts":alerts,"failures":failures},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
