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
    km_match = re.search(r"(?<![/\d])(\d{1,3}(?:[ .]\d{3})+|\d+)\s*km\b", text, re.I)
    kms = int(re.sub(r"\D", "", km_match.group(1))) if km_match else None
    ym = re.search(r"\b(0[1-9]|1[0-2])/(20\d{2})\b", text)
    year = ym.group(0) if ym else None
    fuel = next((x for x in ["Híbrido Plug-In", "Eléctrico", "Elétrico", "Diesel", "Gasolina", "Híbrido"]
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
    v = {"title": title[:180], "year": year, "km": kms, "fuel": fuel, "price": price,
         "url": url, "vin": vin, "registration": registration}
    if vin:
        key = "vin:" + vin
    elif registration:
        key = "reg:" + registration
    else:
        key = "|".join([clean_name(title), year or "", clean_name(fuel or "")])
    v["id"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    v["match_key"] = key
    return v

def parse_mh33_detail(soup, url):
    # MH33CAR detail pages can expose the vehicle data in labels, meta tags,
    # data-* attributes or embedded JSON/JS while the visible page remains a
    # very small client-side shell. Extract from all of those sources.
    raw = str(soup)

    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""
    title_fallback = re.sub(r"^MH33car\s*-\s*", "", norm(title_tag), flags=re.I)
    title_fallback = re.sub(r"\s+em\s+Vila Nova de Gaia.*$", "", title_fallback, flags=re.I).strip()

    texts = [norm(soup.get_text(" ", strip=True))]

    for tag in soup.find_all(["meta", "h1", "h2", "h3", "span", "div", "li"]):
        val = norm(tag.get("content", "") if tag.name == "meta" else tag.get_text(" ", strip=True))
        if val and len(val) <= 500:
            texts.append(val)

        # Some dealer platforms keep the actual value in data-* attributes.
        for attr, value in tag.attrs.items():
            if attr.startswith("data-") and isinstance(value, str):
                value = norm(value)
                if value:
                    texts.append(value)

    for script in soup.find_all("script"):
        raw_script = script.string or script.get_text()
        if raw_script:
            texts.append(norm(raw_script))

    # Also search the raw HTML. HTML entities and escaped JSON are handled by
    # the BeautifulSoup-derived text above, while this catches attribute-level
    # values that are not rendered as text.
    all_text = " ".join(texts)
    raw_lower = raw.lower()

    def first_num(patterns, source):
        for pat in patterns:
            m = re.search(pat, source, re.I | re.S)
            if m:
                return m.group(1)
        return None

    # Prefer values tied to explicit field names. This avoids accidentally
    # picking the numeric ID from the URL or unrelated framework data.
    price_raw = first_num([
        r'(?:"price"|"preco"|"preço"|"sellingPrice"|"salePrice")\s*[:=]\s*["\']?([0-9][0-9 .]*)(?:,[0-9]+|\.[0-9]+)?',
        r'(?:preço|preco|price|valor|selling\s*price|sale\s*price)\s*[:=]?\s*([0-9][0-9 .]*)(?:,[0-9]+|\.[0-9]+)?\s*€',
        r'(?:preço|preco|price|valor)\s*[^0-9]{0,40}([0-9][0-9 .]*)(?:,[0-9]+|\.[0-9]+)?\s*€',
        r'([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{4,6})\s*(?:€|EUR)'
    ], all_text + " " + raw_lower)

    km_raw = first_num([
        r'(?:"mileage"|"kilometers"|"kilometres"|"quilometros"|"quilómetros"|"kms"|"km")\s*[:=]\s*["\']?([0-9][0-9 .]*)',
        r'(?:quilómetros|quilometros|kilometers|kilometres|quilometragem|kms?|km)\s*[:=]?\s*([0-9][0-9 .]*)',
        r'([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{4,7})\s*(?:km|kms|quilómetros|quilometros)\b'
    ], all_text + " " + raw_lower)

    year = None
    ym = re.search(r'\b(0[1-9]|1[0-2])/(20\d{2})\b', all_text)
    if ym:
        year = ym.group(0)
    else:
        # Dealer pages commonly show the registration/month-year first. If it
        # is absent, use a standalone vehicle year from the page.
        years = re.findall(r'\b(20(?:0\d|1\d|2[0-9]))\b', all_text)
        if years:
            year = years[0]

    fuel = next((x for x in ["Híbrido Plug-In", "Eléctrico", "Elétrico", "Diesel", "Gasolina", "Híbrido"]
                 if x.lower() in all_text.lower()), None)

    if price_raw and km_raw:
        price = int(re.sub(r"\D", "", price_raw))
        kms = int(re.sub(r"\D", "", km_raw))

        title = title_fallback
        if len(title) < 4 or title.lower().startswith(("viaturas", "ordenar por", "intermediação")):
            title = re.sub(r"-ID\d+\.html$", "", url.rstrip("/").rsplit("/", 1)[-1], flags=re.I)
            title = re.sub(r"[-_]+", " ", title).strip()

        if len(title) >= 4:
            key = "|".join([clean_name(title), year or "", clean_name(fuel or "")])
            return {
                "title": title[:180], "year": year, "km": kms, "fuel": fuel,
                "price": price, "url": url, "vin": None, "registration": None,
                "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:20],
                "match_key": key
            }

    return None


def extract_stock(soup, url):
    out, seen_ids, seen_urls = [], set(), set()
    anchors = soup.find_all("a", href=lambda h: h and ("/viaturas/" in h or "/viatura/" in h))
    if not anchors:
        anchors = soup.find_all("a", href=True)
    for a in anchors:
        href = urljoin(url, a["href"])
        if href in seen_urls:
            continue
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
    diagnostics = []
    browser_first = any(host in url for host in ["cemporcentocar.pt", "mh33car.pt"])

    if not browser_first:
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
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            page = browser.new_page(user_agent=HEAD["User-Agent"], viewport={"width": 1440, "height": 1000}, locale="pt-PT")
            page.goto(url, wait_until="domcontentloaded", timeout=CFG["timeout_seconds"] * 1000)
            page.wait_for_timeout(3000)

            if "f2car.com" in url:
                for _ in range(8):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
                    for label in ["Ver mais", "Mostrar mais", "Carregar mais", "Mais viaturas"]:
                        try:
                            loc = page.get_by_text(label, exact=False).last
                            if loc.is_visible():
                                loc.click(timeout=800)
                                page.wait_for_timeout(1200)
                        except Exception:
                            pass

            page_stocks = []
            if "cemporcentocar.pt" in url:
                # 100%Car uses client-side numeric pagination. The URL can stay
                # unchanged after clicking page 2, so detect page changes by
                # the vehicle URLs/signature rather than by the browser URL.
                seen_page_signatures = set()
                for page_no in range(1, 13):
                    current_html = page.content()
                    current_stock = extract_stock(BeautifulSoup(current_html, "html.parser"), page.url)
                    signature = tuple(sorted(v["url"] for v in current_stock))
                    if signature in seen_page_signatures:
                        break
                    seen_page_signatures.add(signature)
                    page_stocks.extend(current_stock)

                    target = str(page_no + 1)
                    clicked = False
                    try:
                        candidates = page.get_by_text(target, exact=True)
                        count = candidates.count()
                        for idx in range(count - 1, -1, -1):
                            pager = candidates.nth(idx)
                            if pager.is_visible():
                                pager.click(timeout=2500)
                                page.wait_for_timeout(1800)
                                clicked = True
                                break
                    except Exception:
                        clicked = False
                    if not clicked:
                        break

                merged, seen = [], set()
                for v in page_stocks:
                    if v["id"] not in seen:
                        merged.append(v)
                        seen.add(v["id"])
                stock = merged
            elif "mh33car.pt" in url:
                # MH33CAR exposes the stock index at /viaturas, with singular
                # /viatura/... detail URLs. The index card can contain global
                # filter text, so parse each detail page instead of the parent
                # container.
                if "/viaturas" not in page.url:
                    try:
                        page.goto("https://www.mh33car.pt/viaturas",
                                  wait_until="domcontentloaded",
                                  timeout=CFG["timeout_seconds"] * 1000)
                        page.wait_for_timeout(1800)
                    except Exception:
                        pass

                soup = BeautifulSoup(page.content(), "html.parser")
                detail_urls = []
                seen_urls = set()
                for a in soup.find_all("a", href=True):
                    href = urljoin(page.url, a.get("href", ""))
                    if "/viatura/" in href and href not in seen_urls:
                        seen_urls.add(href)
                        detail_urls.append(href)

                stock = []
                for detail_url in detail_urls[:80]:
                    try:
                        page.goto(detail_url, wait_until="domcontentloaded",
                                  timeout=CFG["timeout_seconds"] * 1000)
                        page.wait_for_timeout(500)
                        detail_soup = BeautifulSoup(page.content(), "html.parser")
                        v = parse_mh33_detail(detail_soup, detail_url)
                        if v:
                            stock.append(v)
                    except Exception:
                        pass
                merged, seen = [], set()
                for v in stock:
                    if v["id"] not in seen:
                        merged.append(v)
                        seen.add(v["id"])
                stock = merged
            else:
                gaia_soup = BeautifulSoup(page.content(), "html.parser")
                stock = extract_stock(gaia_soup, page.url)
                if not stock and "gaiaconceptcar.pt" in url:
                    stock = extract_gaia_stock_from_links(gaia_soup, page.url)

            title = page.title()
            final_url = page.url
            body_text = norm(page.locator("body").inner_text(timeout=5000))
            diagnostics.append(f"chromium_final={final_url} title={title!r} html_bytes={len(page.content())} body_chars={len(body_text)}")
            diagnostics.append(f"chromium_stock={len(stock)}")

            if not stock:
                candidates = []
                soup = BeautifulSoup(page.content(), "html.parser")
                for a in soup.find_all("a", href=True):
                    href = urljoin(final_url, a.get("href", ""))
                    txt = norm(a.get_text(" ", strip=True))
                    blob = (txt + " " + href).lower()
                    if any(k in blob for k in ["/viaturas", "/carros", "/stock", "viatura", "carros"]):
                        candidates.append({"text": txt[:100], "href": href})
                    if len(candidates) >= 30:
                        break
                diagnostics.append("chromium_links=" + json.dumps(candidates, ensure_ascii=False))
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
        print(json.dumps({"mode": "base", "status": "verification_failed", "site": site["name"], "url": site["url"],
                          "reason": "No vehicle cards could be extracted from HTTP or Chromium", "diagnostics": diagnostics}, ensure_ascii=False))
        return 1
    snapshot = {"created_at": now, "source": site["url"], "count": len(stock), "stock": stock}
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
                        changes[field] = {"f2_base": f2.get(field), "supplier_now": v.get(field)}
                if changes:
                    alerts.append({"type": "change", "supplier": supplier, "vehicle": v, "f2_vehicle": f2, "changes": changes})
            else:
                ukey = f"{supplier}::{v['id']}"
                if ukey not in known_unpublished:
                    alerts.append({"type": "new", "supplier": supplier, "vehicle": v})
                known_unpublished[ukey] = now

        for f2id, suppliers_seen in known_suppliers.items():
            f2 = base_by_id.get(f2id)
            if not f2 or supplier not in suppliers_seen:
                continue
            pkey = f"{supplier}::{f2id}"
            if f2["match_key"] in current_keys:
                pending.pop(pkey, None)
            else:
                item = pending.get(pkey, {"count": 0, "vehicle": f2, "first_missing": now})
                item["count"] = int(item.get("count", 0)) + 1
                item["last_missing"] = now
                pending[pkey] = item

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
            alerts.append({"type": "removed", "supplier": supplier, "vehicle": vehicle,
                           "last_confirmed_at": item.get("first_missing"), "f2_url": vehicle.get("url")})
            pending.pop(pkey, None)

    snapshot = {"checked_at": now, "base_date": day, "base_count": len(base_stock),
                "suppliers": suppliers, "failures": failures, "alerts": alerts}
    save_json(os.path.join(BASE_DIR, f"{day}-check.json"), snapshot)
    save_json(os.path.join(DATA, "last_alerts.json"), {"generated_at": now, "base_date": day,
                                                        "alerts": alerts, "failures": failures})
    state["last_check"] = now
    state["pending_removals"] = pending
    state["known_suppliers"] = known_suppliers
    state["known_unpublished"] = known_unpublished
    save_json(state_path, state)

    print(json.dumps({"mode": "check", "status": "ok", "base_count": len(base_stock),
                      "supplier_counts": {k: len(v) for k, v in suppliers.items()},
                      "alerts": alerts, "failures": failures}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(make_base() if MODE == "base" else make_check())
