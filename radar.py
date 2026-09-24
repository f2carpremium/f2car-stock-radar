import json, re, hashlib, os, sys
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

DATA = os.path.join(ROOT, "data")
BASE_DIR = os.path.join(DATA, "stock")
os.makedirs(BASE_DIR, exist_ok=True)

HEAD = {"User-Agent": "Mozilla/5.0 F2CAR-Stock-Radar/2.0"}
MODE = sys.argv[1].lower() if len(sys.argv) > 1 else "check"


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def number(pattern, s):
    m = re.search(pattern, s or "", re.I)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def clean_name(s):
    s = norm(s).lower()
    s = re.sub(r"[^a-z0-9à-ÿ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(text, url):
    text = norm(text)
    price = number(r"(\d[\d .]*)\s*€", text)
    kms = number(r"(\d[\d .]*)\s*km", text)
    ym = re.search(r"\b(0[1-9]|1[0-2])/(20\d{2})\b", text)
    year = ym.group(0) if ym else None
    fuel = next((x for x in ["Híbrido Plug-In", "Elétrico", "Diesel", "Gasolina", "Híbrido"]
                 if x.lower() in text.lower()), None)
    if price is None or kms is None:
        return None

    title = re.split(
        r"\b(?:0[1-9]|1[0-2])/20\d{2}\b|\b\d[\d .]*\s*km\b|\b\d[\d .]*\s*€",
        text, 1
    )[0].strip(" -|")
    if len(title) < 4:
        return None

    # Prefer stable identifiers when a page exposes one.
    vin = None
    m = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", text.upper())
    if m:
        vin = m.group(1)

    registration = None
    m = re.search(r"\b([0-9]{2}-[0-9]{2}-[A-Z]{2}|[A-Z]{2}-[0-9]{2}-[A-Z]{2})\b", text.upper())
    if m:
        registration = m.group(1)

    v = {
        "title": title[:180],
        "year": year,
        "km": kms,
        "fuel": fuel,
        "price": price,
        "url": url,
        "vin": vin,
        "registration": registration
    }

    if vin:
        key = "vin:" + vin
    elif registration:
        key = "reg:" + registration
    else:
        key = "|".join([
            clean_name(title),
            year or "",
            clean_name(fuel or "")
        ])

    v["id"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    v["match_key"] = key
    return v


def scrape(url):
    try:
        r = requests.get(url, headers=HEAD, timeout=CFG["timeout_seconds"])
        if not r.ok:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        out, seen_ids, seen_urls = [], set(), set()

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            text = norm(a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True))
            if href in seen_urls or len(text) < 30 or len(text) > 1200:
                continue
            v = parse(text, href)
            if v and v["id"] not in seen_ids:
                out.append(v)
                seen_ids.add(v["id"])
                seen_urls.add(href)
        return out
    except Exception:
        return None


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def base_path(day):
    return os.path.join(BASE_DIR, f"{day}-base.json")


def latest_base_path():
    return os.path.join(BASE_DIR, "base-latest.json")


def check_path(day):
    return os.path.join(BASE_DIR, f"{day}-check.json")


def make_base():
    site = CFG["sites"]["f2car"]
    stock = scrape(site["url"])
    now = datetime.now(timezone.utc).isoformat()

    if stock is None or len(stock) == 0:
        print(json.dumps({"mode": "base", "status": "verification_failed", "site": site["name"]}))
        return 1

    snapshot = {
        "created_at": now,
        "source": site["url"],
        "count": len(stock),
        "stock": stock
    }

    save_json(base_path(today()), snapshot)
    save_json(latest_base_path(), snapshot)

    # Reset daily pending confirmations because the daily base is the reference point.
    state = load_json(os.path.join(DATA, "state.json"), {})
    state["daily_base"] = today()
    state["pending_removals"] = {}
    save_json(os.path.join(DATA, "state.json"), state)

    print(json.dumps({"mode": "base", "status": "ok", "count": len(stock), "date": today()}, ensure_ascii=False))
    return 0


def match_supplier_to_base(base_stock, supplier_stock):
    base_by_key = {x["match_key"]: x for x in base_stock}
    result = []
    for v in supplier_stock:
        f2 = base_by_key.get(v["match_key"])
        result.append((v, f2))
    return result


def make_check():
    day = today()
    base = load_json(base_path(day), None)
    if not base:
        print(json.dumps({"mode": "check", "status": "base_missing", "date": day}))
        return 1

    base_stock = base["stock"]
    suppliers = {}
    failures = []

    for key, site in CFG["sites"].items():
        if key == "f2car":
            continue
        stock = scrape(site["url"])
        if stock is None:
            failures.append(site["name"])
        else:
            suppliers[site["name"]] = stock

    alerts = []
    now = datetime.now(timezone.utc).isoformat()

    # Map every base F2CAR car to the supplier where it is currently found.
    base_supplier_map = {}
    for supplier, stock in suppliers.items():
        for v, f2 in match_supplier_to_base(base_stock, stock):
            if f2:
                base_supplier_map.setdefault(f2["id"], []).append((supplier, v))

    # New entries and relevant changes.
    for supplier, stock in suppliers.items():
        for v in stock:
            f2 = next((x for x in base_stock if x["match_key"] == v["match_key"]), None)
            if not f2:
                alerts.append({
                    "type": "new",
                    "supplier": supplier,
                    "vehicle": v
                })
                continue

            changes = {}
            for field in ["price", "km", "year", "fuel"]:
                if f2.get(field) != v.get(field):
                    changes[field] = {"f2_base": f2.get(field), "supplier_now": v.get(field)}
            if changes:
                alerts.append({
                    "type": "change",
                    "supplier": supplier,
                    "vehicle": v,
                    "f2_vehicle": f2,
                    "changes": changes
                })

    # A vehicle is considered removed only after two successful supplier checks.
    # Since checks are daily, pending state carries across days.
    state_path = os.path.join(DATA, "state.json")
    state = load_json(state_path, {"pending_removals": {}})
    pending = state.get("pending_removals", {})

    current_supplier_keys = {
        supplier: {v["match_key"] for v in stock}
        for supplier, stock in suppliers.items()
    }

    for f2 in base_stock:
        matches = base_supplier_map.get(f2["id"], [])
        if not matches:
            # We don't know the supplier if the car was not matched at all.
            # Never call this a removal.
            continue

        for supplier, matched in matches:
            key = f"{day}|{supplier}|{f2['id']}"
            # The vehicle was seen at least once at this supplier during today's check.
            if f2["match_key"] in current_supplier_keys.get(supplier, set()):
                pending.pop(key, None)

    # Persist supplier snapshots and today's alerts.
    snapshot = {
        "checked_at": now,
        "base_date": day,
        "base_count": len(base_stock),
        "suppliers": suppliers,
        "failures": failures,
        "alerts": alerts
    }

    save_json(check_path(day), snapshot)
    save_json(os.path.join(DATA, "last_alerts.json"), {
        "generated_at": now,
        "base_date": day,
        "alerts": alerts,
        "failures": failures
    })

    state["last_check"] = now
    state["pending_removals"] = pending
    save_json(state_path, state)

    print(json.dumps({
        "mode": "check",
        "status": "ok",
        "base_count": len(base_stock),
        "supplier_counts": {k: len(v) for k, v in suppliers.items()},
        "alerts": alerts,
        "failures": failures
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(make_base() if MODE == "base" else make_check())
