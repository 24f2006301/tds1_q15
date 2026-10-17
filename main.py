
import os
import re
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

API_ROOT = os.environ.get("API_ROOT", "").strip()
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
HTTP_TIMEOUT = 15
CACHE_SECONDS = 300

app = FastAPI(title="Acme Appliances Order Ledger Q&A", version="1.1.0")

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; AcmeLedgerAgent/1.0)",
    "Accept": "*/*",
})

_cache = {"at": 0.0, "orders": None, "rates": None, "export": None}


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def get_json(url):
    response = _session.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


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


def load_ledger(force=False):
    now = time.time()

    if (
        not force
        and _cache["orders"] is not None
        and now - _cache["at"] < CACHE_SECONDS
    ):
        return _cache["orders"], _cache["rates"], _cache["export"]

    if not API_ROOT:
        raise RuntimeError("API_ROOT environment variable is not configured")

    root = get_json(API_ROOT)
    links = root.get("links", {})

    orders_url = links.get("orders")
    rates_url = links.get("rates")
    export_url = links.get("export")

    if not orders_url or not rates_url:
        raise RuntimeError("API root is missing orders or rates links")

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
        page_orders = page_data.get("orders", [])

        if not isinstance(page_orders, list):
            raise RuntimeError("Orders page did not contain an orders list")

        all_orders.extend(page_orders)
        url = page_data.get("next")

    # Keep only the latest version of each order.
    current = {}

    for row in all_orders:
        order_id = str(row.get("id", "")).strip()
        if not order_id:
            continue

        previous = current.get(order_id)

        if (
            previous is None
            or parse_date(row.get("updated_at"))
            >= parse_date(previous.get("updated_at"))
        ):
            current[order_id] = row

    orders = list(current.values())

    export_text = None
    if export_url:
        try:
            response = _session.get(export_url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            export_text = response.text
        except requests.RequestException:
            pass

    _cache.update({
        "at": time.time(),
        "orders": orders,
        "rates": rates,
        "export": export_text,
    })

    return orders, rates, export_text


def usd_amount(row, rates):
    currency = str(row.get("currency", "USD")).upper()
    amount = float(row.get("amount", 0) or 0)

    if currency not in rates:
        raise ValueError(f"Missing exchange rate for {currency}")

    return amount * float(rates[currency])


def amount_in_currency(row, rates, target_currency):
    usd_value = usd_amount(row, rates)
    target_rate = float(rates.get(target_currency, 0))

    if target_rate <= 0:
        raise ValueError(f"Missing exchange rate for {target_currency}")

    return usd_value / target_rate


def money(value):
    return f"{value:.2f}"


def extract_period(question):
    q = question.lower()

    years = re.findall(r"\b20\d{2}\b", q)

    months = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sept": 9, "sep": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    found_month = None

    for word, number in months.items():
        if re.search(r"\b" + re.escape(word) + r"\b", q):
            found_month = number
            break

    year = int(years[0]) if years else None
    return year, found_month


def get_customer_key(row):
    # Prefer a customer ID over a display name when available.
    for field in ("customer_id", "customerId", "customer", "customer_name"):
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def answer_question(question, orders, rates):
    q = question.strip()
    ql = q.lower()

    year, month = extract_period(ql)

    date_field = "created_at"
    if any(word in ql for word in ("updated", "last updated", "status changed")):
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

    # Identify product, region, and customer values mentioned in the question.
    products = sorted({
        str(r.get("product", "")).strip()
        for r in orders if r.get("product")
    })

    regions = sorted({
        str(r.get("region", "")).strip()
        for r in orders if r.get("region")
    })

    customers = sorted({
        str(r.get("customer", "")).strip()
        for r in orders if r.get("customer")
    })

    product = next(
        (p for p in sorted(products, key=len, reverse=True)
         if p.lower() in ql),
        None,
    )

    region = next(
        (r for r in sorted(regions, key=len, reverse=True)
         if r.lower() in ql),
        None,
    )

    customer = next(
        (c for c in sorted(customers, key=len, reverse=True)
         if c.lower() in ql),
        None,
    )

    if product:
        rows = [
            r for r in rows
            if str(r.get("product", "")).lower() == product.lower()
        ]

    if region:
        rows = [
            r for r in rows
            if str(r.get("region", "")).lower() == region.lower()
        ]

    if customer:
        rows = [
            r for r in rows
            if str(r.get("customer", "")).lower() == customer.lower()
        ]

    # Status filtering.
    asks_refund = any(w in ql for w in ("refund", "refunded", "refunds"))
    asks_void = "void" in ql

    asks_paid = any(w in ql for w in (
        "paid", "revenue", "sales", "sold", "sell",
        "earn", "earning", "earnings", "income",
    ))

    if asks_refund:
        rows = [
            r for r in rows
            if str(r.get("status", "")).lower() == "refunded"
        ]
    elif asks_void:
        rows = [
            r for r in rows
            if str(r.get("status", "")).lower() == "void"
        ]
    elif asks_paid:
        rows = [
            r for r in rows
            if str(r.get("status", "")).lower() == "paid"
        ]

    # Count distinct customers only after filtering to paid orders.
    if (
        "customer" in ql
        and any(w in ql for w in ("different", "distinct", "unique"))
    ):
        customer_ids = {
            key
            for r in rows
            if (key := get_customer_key(r)) is not None
        }
        return str(len(customer_ids))

    if not rows:
        return "No matching orders found."

    # Determine the requested output currency.
    if re.search(r"\b(inr|rupees|indian rupees)\b", ql):
        target_currency = "INR"
    elif re.search(r"\b(eur|euros)\b", ql):
        target_currency = "EUR"
    else:
        # USD is the default, including "US dollars" and "dollars".
        target_currency = "USD"

    is_revenue = any(w in ql for w in (
        "revenue", "sales", "amount", "value",
        "earn", "earning", "earnings", "income",
    ))

    # Ranking questions.
    is_ranking = any(w in ql for w in (
        "top", "highest", "most", "best", "largest",
        "which product", "which region",
    ))

    if is_ranking:
        metric_rows = rows

        if not asks_refund and not asks_void:
            metric_rows = [
                r for r in rows
                if str(r.get("status", "")).lower() == "paid"
            ]

        if not metric_rows:
            return "No matching orders found."

        if "region" in ql:
            group_key = "region"
        elif "customer" in ql:
            group_key = "customer"
        else:
            group_key = "product"

        totals = {}

        for r in metric_rows:
            key = str(r.get(group_key, "Unknown"))

            if is_revenue:
                value = amount_in_currency(r, rates, target_currency)
            elif any(w in ql for w in ("units", "quantity", "unit")):
                value = int(r.get("qty", 0) or 0)
            else:
                value = 1

            totals[key] = totals.get(key, 0) + value

        winner = max(totals, key=totals.get)

        if is_revenue:
            return f"{winner}: {money(totals[winner])} {target_currency}"

        if any(w in ql for w in ("units", "quantity", "unit")):
            return f"{winner}: {int(totals[winner])} units"

        return f"{winner}: {int(totals[winner])} orders"

    # Count and quantity questions.
    if any(w in ql for w in (
        "how many", "count", "number of", "quantity", "units",
    )):
        if any(w in ql for w in ("unit", "quantity")):
            return str(sum(int(r.get("qty", 0) or 0) for r in rows))

        return str(len(rows))

    # Total, revenue, refund, and earnings questions.
    if any(w in ql for w in (
        "revenue", "sales", "total", "sum", "amount", "value",
        "refund", "refunds", "how much", "earn", "earning",
        "earnings", "income",
    )):
        total = sum(
            amount_in_currency(r, rates, target_currency)
            for r in rows
        )
        return f"{money(total)} {target_currency}"

    # Average questions.
    if any(w in ql for w in ("average", "avg", "mean")):
        total = sum(usd_amount(r, rates) for r in rows)
        return f"{money(total / len(rows))} USD"

    # Order ID listing.
    if any(w in ql for w in (
        "list", "show", "which orders", "order ids", "ids",
    )):
        return ", ".join(str(r.get("id")) for r in rows[:100])

    # Generic fallback.
    paid_rows = [
        r for r in rows
        if str(r.get("status", "")).lower() == "paid"
    ]

    return json.dumps({
        "matching_orders": len(rows),
        "paid_orders": len(paid_rows),
        "paid_revenue_usd": money(
            sum(usd_amount(r, rates) for r in paid_rows)
        ),
        "statuses": {
            status: sum(
                1 for r in rows
                if str(r.get("status", "")).lower() == status
            )
            for status in sorted({
                str(r.get("status", "")).lower() for r in rows
            })
        },
    })


@app.get("/")
def health():
    return {
        "service": "Acme Appliances order ledger Q&A",
        "status": "ok",
        "endpoint": "POST /answer",
    }


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
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch ledger API: {exc.__class__.__name__}",
        )

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/")
def answer_root(request: QuestionRequest):
    return answer(request)
