import json, re, hashlib, os, sys
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

DATA = os.path.join(ROOT, "data")
BASE_DIR = os.path.join(DATA, "stock")
os.makedirs(BASE_DIR, exist_ok=True)

HEAD = {"User-Agent": "Mozilla/5.0 F2CAR-Stock-Radar/2.1"}
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
    prices = re.findall(r"(\d[\d .]*)\s*€", text)
    price = int(re.sub(r"\D", "", prices[-1])) if prices else None
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
        key = "|".join([clean_name(title), year or "", clean_name(fuel or "")])

    v["id"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    v["match_key"] = key
    return v

def extract_stock(soup, url):
    out, seen_ids, seen_urls = [], set(), set()

    # Vehicle detail links are the most reliable anchors on F2CAR.
    anchors = soup.find_all("a", href=lambda h: h and "/viaturas/" in h)
    if not anchors:
        anchors = soup.find_all("a", href=True)

    for a in anchors:
        href = urljoin(url, a["href"])
        if href in seen_urls:
            continue

        # Prefer the anchor text, then walk up a few levels to recover the
        # complete vehicle card when the anchor only contains the model name.
        candidates = [norm(a.get_text(" ", strip=True))]
        node = a.parent
        for _ in range(4):
            if node:
                candidates.append(norm(node.get_text(" ", strip=True)))
                node = node.parent

        v = None
        for text in candidates:
            if len(text) < 20 or len(text) > 2000:
                continue
            candidate = parse(text, href)
            if candidate:
                v = candidate
                break

        if v and v["id"] not in seen_ids:
            out.append(v)
            seen_ids.add(v["id"])
            seen_urls.add(href)

    return out

def scrape(url):
    # Try ordinary HTML first; use Chromium when the site renders stock with JavaScript.
    diagnostics = []

    try:
        r = requests.get(url, headers=HEAD, timeout=CFG["timeout_seconds"])
        diagnostics.append(f"http={r.status_code} bytes={len(r.content)} final={r.url}")
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            stock = extract_stock(soup, r.url)
            diagnostics.append(f"http_stock={len(stock)} title={norm(soup.title.get_text()) if soup.title else ''}")
            if stock:
                return stock, diagnostics
    except Exception as e:
        diagnostics.append(f"http_error={type(e).__name__}:{e}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = browser.new_page(
                user_agent=HEAD["User-Agent"],
                viewport={"width": 1440, "height": 1000},
                locale="pt-PT"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=CFG["timeout_seconds"] * 1000)
            page.wait_for_timeout(5000)
            html = page.content()
            title = page.title()
            final_url = page.url
            body_text = norm(page.locator("body").inner_text(timeout=5000))
            diagnostics.append(
                f"chromium_final={final_url} title={title!r} html_bytes={len(html)} body_chars={len(body_text)}"
            )
            stock = extract_stock(BeautifulSoup(html, "html.parser"), final_url)
            diagnostics.append(f"chromium_stock={len(stock)}")
            browser.close()
        if stock:
            return stock, diagnostics
    except Exception as e:
        diagnostics.append(f"chromium_error={type(e).__name__}:{e}")

    return None, diagnostics

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def base_path(day):
    return os.path.join(BASE_DIR, f"{day}-base.json")

def make_base():
    site = CFG["sites"]["f2car"]
    stock, diagnostics = scrape(site["url"])
    now = datetime.now(timezone.utc).isoformat()
    if stock is None or len(stock) == 0:
        print(json.dumps({
            "mode": "base",
            "status": "verification_failed",
            "site": site["name"],
            "url": site["url"],
            "reason": "No vehicle cards could be extracted from HTTP or Chromium",
            "diagnostics": diagnostics
        }, ensure_ascii=False))
        return 1

    snapshot = {
        "created_at": now,
        "source": site["url"],
        "count": len(stock),
        "stock": stock
    }
    save_json(base_path(today()), snapshot)
    save_json(os.path.join(BASE_DIR, "base-latest.json"), snapshot)

    state_path = os.path.join(DATA, "state.json")
    state = load_json(state_path, {})
    state["daily_base"] = today()
    save_json(state_path, state)

    print(json.dumps({"mode": "base", "status": "ok", "count": len(stock), "date": today()}, ensure_ascii=False))
    return 0

def make_check():
    day = today()
    base = load_json(base_path(day), None)
    if not base:
        print(json.dumps({"mode": "check", "status": "base_missing", "date": day}))
        return 1

    base_stock = base["stock"]
    suppliers, failures = {}, []

    for key, site in CFG["sites"].items():
        if key == "f2car":
            continue
        stock, diagnostics = scrape(site["url"])
        if stock is None:
            failures.append({"site": site["name"], "diagnostics": diagnostics})
        else:
            suppliers[site["name"]] = stock

    state_path = os.path.join(DATA, "state.json")
    state = load_json(state_path, {"pending_removals": {}, "known_suppliers": {}, "known_unpublished": {}})
    known_suppliers = state.get("known_suppliers", {})
    pending = state.get("pending_removals", {})
    known_unpublished = state.get("known_unpublished", {})

    base_by_key = {v["match_key"]: v for v in base_stock}
    base_by_id = {v["id"]: v for v in base_stock}
    alerts = []
    now = datetime.now(timezone.utc).isoformat()

    for supplier, stock in suppliers.items():
        current_keys = {v["match_key"] for v in stock}

        for v in stock:
            f2 = base_by_key.get(v["match_key"])
            if f2:
                known_suppliers.setdefault(f2["id"], [])
                if supplier not in known_suppliers[f2["id"]]:
                    known_suppliers[f2["id"]].append(supplier)

                changes = {}
                for field in ["price", "km", "year", "fuel"]:
                    if f2.get(field) != v.get(field):
                        changes[field] = {
                            "f2_base": f2.get(field),
                            "supplier_now": v.get(field)
                        }
                if changes:
                    alerts.append({
                        "type": "change",
                        "supplier": supplier,
                        "vehicle": v,
                        "f2_vehicle": f2,
                        "changes": changes
                    })
            else:
                ukey = f"{supplier}::{v['id']}"
                if ukey not in known_unpublished:
                    alerts.append({
                        "type": "new",
                        "supplier": supplier,
                        "vehicle": v
                    })
                known_unpublished[ukey] = now

        # Start/clear removal confirmations for vehicles in today's F2CAR base.
        for f2id, suppliers_seen in known_suppliers.items():
            f2 = base_by_id.get(f2id)
            if not f2 or supplier not in suppliers_seen:
                continue
            pkey = f"{supplier}::{f2id}"
            if f2["match_key"] in current_keys:
                pending.pop(pkey, None)
            else:
                item = pending.get(pkey, {
                    "count": 0,
                    "vehicle": f2,
                    "first_missing": now
                })
                item["count"] = int(item.get("count", 0)) + 1
                item["last_missing"] = now
                pending[pkey] = item

    # Continue pending confirmations even if the vehicle is no longer in the
    # next F2CAR base. This makes the second verification robust.
    for pkey, item in list(pending.items()):
        try:
            supplier, f2id = pkey.split("::", 1)
        except ValueError:
            continue
        if supplier not in suppliers:
            continue
        current_keys = {v["match_key"] for v in suppliers[supplier]}
        vehicle = item.get("vehicle", {})
        if vehicle.get("match_key") in current_keys:
            pending.pop(pkey, None)
            continue
        if int(item.get("count", 0)) >= CFG["removal_confirmations"]:
            alerts.append({
                "type": "removed",
                "supplier": supplier,
                "vehicle": vehicle,
                "last_confirmed_at": item.get("first_missing"),
                "f2_url": vehicle.get("url")
            })
            pending.pop(pkey, None)

    snapshot = {
        "checked_at": now,
        "base_date": day,
        "base_count": len(base_stock),
        "suppliers": suppliers,
        "failures": failures,
        "alerts": alerts
    }
    save_json(os.path.join(BASE_DIR, f"{day}-check.json"), snapshot)
    save_json(os.path.join(DATA, "last_alerts.json"), {
        "generated_at": now,
        "base_date": day,
        "alerts": alerts,
        "failures": failures
    })

    state["last_check"] = now
    state["pending_removals"] = pending
    state["known_suppliers"] = known_suppliers
    state["known_unpublished"] = known_unpublished
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
