import os
import re
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

API_ROOT = os.environ.get("API_ROOT", "").strip()
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
HTTP_TIMEOUT = 8
CACHE_SECONDS = 300

app = FastAPI(title="Acme Appliances Order Ledger Q&A", version="1.0.0")
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AcmeLedgerAgent/1.0)", "Accept": "*/*"})
_cache = {"at": 0.0, "orders": None, "rates": None, "export": None}


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def get_json(url):
    response = _session.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def load_ledger(force=False):
    now = time.time()
    if not force and _cache["orders"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["orders"], _cache["rates"], _cache["export"]

    if not API_ROOT:
        raise RuntimeError("API_ROOT environment variable is not configured")

    root = get_json(API_ROOT)
    links = root.get("links", {})
    orders_url = links.get("orders")
    rates_url = links.get("rates")
    export_url = links.get("export")
    if not orders_url or not rates_url:
        raise RuntimeError("The API root did not return expected orders/rates links")

    rates_payload = get_json(rates_url)
    rates = rates_payload.get("usd_per_unit", {"USD": 1.0})
    all_orders = []
    url = orders_url
    seen_urls = set()
    while url:
        if url in seen_urls:
            raise RuntimeError("Pagination loop detected in orders API")
        seen_urls.add(url)
        page_data = get_json(url)
        rows = page_data.get("orders", [])
        if not isinstance(rows, list):
            raise RuntimeError("Orders page did not contain an orders list")
        all_orders.extend(rows)
        url = page_data.get("next")

    # Keep the row with the latest updated_at for each order ID.
    current = {}
    for row in all_orders:
        order_id = str(row.get("id", ""))
        if not order_id:
            continue
        previous = current.get(order_id)
        if previous is None or parse_date(row.get("updated_at")) >= parse_date(previous.get("updated_at")):
            current[order_id] = row
    orders = list(current.values())

    export_text = None
    if export_url:
        try:
            r = _session.get(export_url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            export_text = r.text
        except requests.RequestException:
            export_text = None

    _cache.update({"at": now, "orders": orders, "rates": rates, "export": export_text})
    return orders, rates, export_text


def parse_date(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def local_date(row, field="created_at"):
    dt = parse_date(row.get(field))
    if dt.year == datetime.min.year:
        return None
    return dt.astimezone(LOCAL_TZ)


def usd_amount(row, rates):
    currency = str(row.get("currency", "USD")).upper()
    amount = float(row.get("amount", 0) or 0)
    return amount * float(rates.get(currency, 1.0))


def money(value):
    # Round only for display; computations retain full precision.
    return f"{value:.2f}"


def extract_period(q):
    ql = q.lower()
    years = re.findall(r"\b20\d{2}\b", ql)
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
        "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12
    }
    found_month = None
    for word, number in months.items():
        if re.search(r"\b" + re.escape(word) + r"\b", ql):
            found_month = number
            break
    year = int(years[0]) if years else None
    return year, found_month


def answer_question(question, orders, rates):
    q = question.strip()
    ql = q.lower()

    # Scope rows to a mentioned calendar year/month, using Asia/Kolkata business dates.
    year, month = extract_period(ql)
    date_field = "created_at"
    if any(w in ql for w in ("updated", "last updated", "status changed")):
        date_field = "updated_at"
    rows = []
    for row in orders:
        dt = local_date(row, date_field)
        if dt is None:
            continue
        if year is not None and dt.year != year:
            continue
        if month is not None and dt.month != month:
            continue
        rows.append(row)

    # Filter common dimensions when the exact API value is present in the question.
    products = sorted({str(r.get("product", "")) for r in orders if r.get("product")})
    regions = sorted({str(r.get("region", "")) for r in orders if r.get("region")})
    currencies = sorted({str(r.get("currency", "")).upper() for r in orders if r.get("currency")})
    customers = sorted({str(r.get("customer", "")) for r in orders if r.get("customer")})
    product = next((p for p in sorted(products, key=len, reverse=True) if p.lower() in ql), None)
    region = next((r for r in sorted(regions, key=len, reverse=True) if r.lower() in ql), None)
    customer = next((c for c in sorted(customers, key=len, reverse=True) if c.lower() in ql), None)
    currency = next((c for c in currencies if re.search(r"\b" + c.lower() + r"\b", ql)), None)

    if product:
        rows = [r for r in rows if r.get("product", "").lower() == product.lower()]
    if region:
        rows = [r for r in rows if r.get("region", "").lower() == region.lower()]
    if customer:
        rows = [r for r in rows if r.get("customer", "").lower() == customer.lower()]

    # Status selection: revenue is strictly paid orders; refunds are reported separately.
    asks_refund = any(w in ql for w in ("refund", "refunded", "refunds"))
    asks_void = "void" in ql
    asks_paid = any(w in ql for w in ("paid", "revenue", "sales", "sold", "earning", "income"))
    if asks_refund:
        rows = [r for r in rows if str(r.get("status", "")).lower() == "refunded"]
    elif asks_void:
        rows = [r for r in rows if str(r.get("status", "")).lower() == "void"]
    elif asks_paid:
        rows = [r for r in rows if str(r.get("status", "")).lower() == "paid"]

    # Ranking questions: return the best matching entity with a metric.
    if any(w in ql for w in ("top", "highest", "most", "best", "largest", "which product", "which region")):
        metric_rows = [r for r in rows if str(r.get("status", "")).lower() == "paid"] if not asks_refund and not asks_void else rows
        if not metric_rows:
            return "No matching orders found."
        group_key = "product" if ("product" in ql or not region and not customer) else ("region" if "region" in ql else "customer")
        totals = {}
        for r in metric_rows:
            key = str(r.get(group_key, "Unknown"))
            value = usd_amount(r, rates) if ("revenue" in ql or "sales" in ql or "amount" in ql or "value" in ql) else 1
            totals[key] = totals.get(key, 0.0) + value
        winner = max(totals, key=totals.get)
        metric = "USD revenue" if ("revenue" in ql or "sales" in ql or "amount" in ql or "value" in ql) else "orders"
        return f"{winner}: {money(totals[winner])} USD revenue" if metric == "USD revenue" else f"{winner}: {int(totals[winner])} orders"

    if not rows:
        return "No matching orders found."

    # Count/quantity questions.
    if any(w in ql for w in ("how many", "count", "number of", "quantity", "units")):
        # "How many different customers..." asks for distinct customer IDs,
        # not the number of matching order rows.
        if "customer" in ql and any(w in ql for w in ("different", "distinct", "unique")):
            return str(len({
                str(r.get("customer", "")).strip()
                for r in rows
                if str(r.get("customer", "")).strip()
            }))
        if "unit" in ql or "quantity" in ql:
            return str(sum(int(r.get("qty", 0) or 0) for r in rows))
        return str(len(rows))

    # Sum amounts in requested currency, or normalize to USD by default.
    if any(w in ql for w in ("revenue", "sales", "total", "sum", "amount", "value", "refund", "refunds", "how much", "earnings", "income")):
        if currency and currency != "USD":
            total = sum(float(r.get("amount", 0) or 0) for r in rows)
            return f"{money(total)} {currency}"
        total = sum(usd_amount(r, rates) for r in rows)
        return f"{money(total)} USD"

    if "average" in ql or "avg" in ql or "mean" in ql:
        total = sum(usd_amount(r, rates) for r in rows)
        return f"{money(total / len(rows))} USD"

    if any(w in ql for w in ("list", "show", "which orders", "order ids", "ids")):
        return ", ".join(str(r.get("id")) for r in rows[:100])

    # Generic fallback returns a compact aggregate rather than hallucinating.
    total_paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
    return json.dumps({
        "matching_orders": len(rows),
        "paid_orders": len(total_paid),
        "paid_revenue_usd": money(sum(usd_amount(r, rates) for r in total_paid)),
        "statuses": {s: sum(1 for r in rows if str(r.get("status", "")).lower() == s)
                     for s in sorted({str(r.get("status", "")).lower() for r in rows})}
    })


@app.get("/")
def health():
    return {"service": "Acme Appliances order ledger Q&A", "status": "ok", "endpoint": "POST /answer"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/answer")
def answer(request: QuestionRequest):
    try:
        orders, rates, _ = load_ledger()
        result = answer_question(request.question, orders, rates)
        return {"answer": result}
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch ledger API: {exc.__class__.__name__}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/")
def answer_root(request: QuestionRequest):
    return answer(request)
