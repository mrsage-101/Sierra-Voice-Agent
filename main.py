"""FreshMart mock grocery API for the Sierra voice/text ordering agent.

In-memory and seeded on startup: restarting the service (or POST /admin/reset)
puts every customer, order and complaint back to its seed state.
All money is USD; all times are America/New_York.
"""

import json
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request, Security
from fastapi.security import APIKeyHeader
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

TZ = ZoneInfo("America/New_York")
SEED = Path(__file__).parent / "seed"
API_KEY = os.environ.get("FRESHMART_API_KEY", "freshmart-dev-key")

MIN_ORDER = 35.00
DELIVERY_FEE = 5.99
FREE_DELIVERY_AT = 100.00
TAX_RATE = 0.08875  # applied to taxable (household) items only
AUTO_CREDIT_LIMIT = 25.00
SERVICE_ZIPS = {"10001", "10002", "10003", "11201"}
WINDOWS = [(8, 10), (10, 12), (12, 14), (14, 16), (16, 18), (18, 20)]
SLOT_DAYS = 3

app = FastAPI(
    title="FreshMart Mock API",
    version="1.0.0",
    description="Mock grocery ordering API for the FreshMart Sierra agent (demo data only).",
)


# ---------- errors ----------

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "INVALID_INPUT", "message": f"{field}: {first.get('msg', 'invalid input')}"}},
    )


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_key(x_api_key: Optional[str] = Security(api_key_header)):
    if x_api_key != API_KEY:
        raise ApiError(401, "UNAUTHORIZED", "Missing or invalid X-API-Key header.")


# ---------- state ----------

CATALOG: dict = {}
CUSTOMERS: dict = {}
ORDERS: dict = {}
COMPLAINTS: dict = {}
COUNTERS = {"order": 10500, "customer": 1100, "complaint": 5100}


def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def money(x: float) -> float:
    return round(x + 1e-9, 2)


def norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11:
        raise ApiError(422, "INVALID_INPUT", "Phone must be a 10-digit US number or E.164 (+1XXXXXXXXXX).")
    return "+" + digits


def slot_id(d: date, start: int) -> str:
    return f"S-{d:%Y%m%d}-{start:02d}"


def parse_slot(sid: str):
    m = re.fullmatch(r"S-(\d{8})-(\d{2})", sid or "")
    if not m:
        return None
    d = datetime.strptime(m.group(1), "%Y%m%d").date()
    start = int(m.group(2))
    match = [w for w in WINDOWS if w[0] == start]
    return (d, match[0]) if match else None


def window_label(start: int, end: int) -> str:
    fmt = lambda h: f"{(h - 1) % 12 + 1}:00 {'am' if h < 12 else 'pm'}"
    return f"{fmt(start)}-{fmt(end)}"


def slot_obj(d: date, w) -> dict:
    return {"slot_id": slot_id(d, w[0]), "date": d.isoformat(), "window": window_label(*w)}


def cutoff_for(d: date) -> datetime:
    return datetime.combine(d - timedelta(days=1), time(22, 0), TZ)


def full_slots() -> set:
    tomorrow = now().date() + timedelta(days=1)
    return {slot_id(tomorrow, 16)}  # tomorrow 4-6 pm is always full (tests SLOT_FULL)


def product_view(p: dict, with_sub: bool = True) -> dict:
    out = {k: p[k] for k in ("sku", "name", "brand", "size", "unit", "price", "in_stock", "aisle", "age_restricted")}
    out["substitute"] = None
    if with_sub and p.get("substitute_sku"):
        s = CATALOG[p["substitute_sku"]]
        out["substitute"] = {k: s[k] for k in ("sku", "name", "brand", "size", "unit", "price", "in_stock")}
    return out


def build_lines(items: list) -> list:
    lines = []
    for it in items:
        p = CATALOG[it["sku"]]
        qty = float(it["qty"])
        qty = int(qty) if qty.is_integer() else qty
        lines.append({
            "sku": p["sku"], "name": p["name"], "brand": p["brand"], "size": p["size"],
            "unit": p["unit"], "qty": qty, "unit_price": p["price"], "line_total": money(p["price"] * qty),
        })
    return lines


def totals(lines: list) -> dict:
    subtotal = money(sum(l["line_total"] for l in lines))
    taxable = sum(l["line_total"] for l in lines if CATALOG[l["sku"]]["taxable"])
    fee = 0.0 if subtotal >= FREE_DELIVERY_AT else DELIVERY_FEE
    tax = money(taxable * TAX_RATE)
    return {"subtotal": subtotal, "delivery_fee": fee, "tax": tax, "total": money(subtotal + fee + tax)}


def order_view(o: dict) -> dict:
    cutoff = datetime.fromisoformat(o["cutoff_at"])
    view = dict(o)
    view["can_modify"] = o["status"] == "placed" and now() < cutoff
    return view


def make_order(oid, cust, items, d, w, status, sub_pref="substitute", pay="card_on_delivery", created=None):
    lines = build_lines(items)
    o = {
        "order_id": oid, "customer_id": cust, "status": status, "items": lines,
        "slot": slot_obj(d, w), **totals(lines),
        "substitution_pref": sub_pref, "payment_method": pay, "pay_link": None,
        "cutoff_at": iso(cutoff_for(d)), "created_at": iso(created or now()),
    }
    ORDERS[oid] = o
    return o


def seed():
    CATALOG.clear(); CUSTOMERS.clear(); ORDERS.clear(); COMPLAINTS.clear()
    COUNTERS.update({"order": 10500, "customer": 1100, "complaint": 5100})
    for p in json.loads((SEED / "catalog.json").read_text()):
        CATALOG[p["sku"]] = p
    for c in json.loads((SEED / "customers.json").read_text()):
        CUSTOMERS[c["customer_id"]] = c

    today = now().date()
    # Sarah: upcoming order, always modifiable (slot in 2 days, cutoff tomorrow 10 pm)
    make_order("FM-10421", "CUST-1001",
               [{"sku": "DAI-001", "qty": 1}, {"sku": "DAI-010", "qty": 1}, {"sku": "BAK-002", "qty": 1},
                {"sku": "PRO-001", "qty": 2}, {"sku": "MEA-003", "qty": 2}, {"sku": "BEV-003", "qty": 1},
                {"sku": "PRO-004", "qty": 1}],
               today + timedelta(days=2), (14, 16), "placed", created=now() - timedelta(hours=3))
    # Priya: out for delivery today (past cutoff)
    hour = min(max(now().hour, 8), 18)
    w = next(x for x in WINDOWS if x[0] <= hour < x[1])
    make_order("FM-10350", "CUST-1003",
               [{"sku": "DAI-002", "qty": 2}, {"sku": "PRO-002", "qty": 3}, {"sku": "MEA-005", "qty": 1},
                {"sku": "PAN-003", "qty": 1}, {"sku": "HOU-001", "qty": 1}, {"sku": "BEV-006", "qty": 1}],
               today, w, "out_for_delivery", created=now() - timedelta(days=2))
    # James: delivered yesterday (complaint under $25 test)
    make_order("FM-10388", "CUST-1002",
               [{"sku": "DAI-006", "qty": 4}, {"sku": "BAK-001", "qty": 1}, {"sku": "MEA-001", "qty": 2},
                {"sku": "PAN-001", "qty": 2}, {"sku": "FRO-001", "qty": 1}, {"sku": "BEV-005", "qty": 1}],
               today - timedelta(days=1), (10, 12), "delivered", created=now() - timedelta(days=3))
    # Tom: delivered 5 days ago, already complained 3 days ago (second complaint -> human)
    make_order("FM-10301", "CUST-1004",
               [{"sku": "MEA-005", "qty": 2}, {"sku": "PRO-006", "qty": 2}, {"sku": "DAI-007", "qty": 1},
                {"sku": "BAK-010", "qty": 1}, {"sku": "PAN-008", "qty": 1}],
               today - timedelta(days=5), (18, 20), "delivered", created=now() - timedelta(days=7))
    COMPLAINTS["CMP-5001"] = {
        "complaint_id": "CMP-5001", "order_id": "FM-10301", "customer_id": "CUST-1004", "sku": "BAK-010",
        "issue": "damaged", "qty": 1, "credit_amount": 5.99, "status": "credited",
        "created_at": iso(now() - timedelta(days=3)),
    }


seed()


# ---------- request models ----------

class Address(BaseModel):
    line1: str = Field(..., examples=["12 Main Street, Apt 3"])
    city: str = Field(..., examples=["New York"])
    state: str = Field(..., examples=["NY"])
    zip: str = Field(..., pattern=r"^\d{5}$", examples=["10001"])


class NewCustomer(BaseModel):
    first_name: str
    last_name: str
    phone: str = Field(..., examples=["+12125550199"])
    address: Address


class LineItem(BaseModel):
    sku: str = Field(..., examples=["DAI-001"])
    qty: float = Field(..., gt=0, description="Count for 'each' items; pounds for 'lb' items.")


class NewOrder(BaseModel):
    customer_id: str
    items: list[LineItem] = Field(..., min_length=1)
    slot_id: str = Field(..., examples=["S-20261102-14"])
    substitution_pref: Literal["substitute", "skip"]
    payment_method: Literal["card_on_delivery", "pay_link"]


class OrderUpdate(BaseModel):
    customer_id: str
    items: Optional[list[LineItem]] = Field(default=None, description="Full replacement item list.")
    slot_id: Optional[str] = None


class CancelRequest(BaseModel):
    customer_id: str
    reason: str = "customer_request"


class NewComplaint(BaseModel):
    customer_id: str
    order_id: str
    sku: str
    issue: Literal["missing", "damaged", "wrong"]
    qty: float = Field(1, gt=0)


# ---------- helpers for validation ----------

def get_customer(cid: str) -> dict:
    c = CUSTOMERS.get(cid)
    if not c:
        raise ApiError(404, "NOT_FOUND", f"No customer with id {cid}.")
    return c


def get_owned_order(oid: str, cid: str) -> dict:
    o = ORDERS.get((oid or "").upper().replace(" ", ""))
    if not o or o["customer_id"] != cid:
        raise ApiError(404, "NOT_FOUND", f"No order {oid} found for this customer.")
    return o


def check_items(items: list[LineItem]) -> list[dict]:
    clean, out_of_stock = [], []
    for it in items:
        p = CATALOG.get(it.sku.upper())
        if not p:
            raise ApiError(404, "NOT_FOUND", f"Unknown product {it.sku}.")
        if p["age_restricted"]:
            raise ApiError(422, "INVALID_INPUT", f"{p['name']} is age-restricted and cannot be ordered for delivery.")
        if not p["in_stock"]:
            out_of_stock.append(p["sku"])
        if p["unit"] == "each" and not float(it.qty).is_integer():
            raise ApiError(422, "INVALID_INPUT", f"{p['name']} is sold by the item; quantity must be a whole number.")
        clean.append({"sku": p["sku"], "qty": it.qty})
    if out_of_stock:
        raise ApiError(409, "OUT_OF_STOCK", "Some items are out of stock.", {"skus": out_of_stock})
    return clean


def check_slot(sid: str, zip_code: str):
    parsed = parse_slot(sid)
    if not parsed:
        raise ApiError(422, "INVALID_INPUT", f"Unknown slot id {sid}.")
    d, w = parsed
    today = now().date()
    if zip_code not in SERVICE_ZIPS:
        raise ApiError(422, "INVALID_INPUT", f"FreshMart does not deliver to ZIP {zip_code}.")
    if not (today < d <= today + timedelta(days=SLOT_DAYS)):
        raise ApiError(422, "INVALID_INPUT", "Slot is not in the bookable range (next 3 days).")
    if sid in full_slots():
        raise ApiError(409, "SLOT_FULL", "That delivery window is full.")
    return d, w


# ---------- endpoints ----------

@app.get("/", tags=["meta"])
def health():
    return {"service": "freshmart-mock-api", "status": "ok", "time": iso(now())}


@app.get("/customers/by-phone", dependencies=[Depends(require_key)], tags=["customers"],
         operation_id="lookup_customer")
def lookup_customer(phone: str = Query(..., examples=["+12125550101"])):
    p = norm_phone(phone)
    for c in CUSTOMERS.values():
        if c["phone"] == p:
            return {k: c[k] for k in ("customer_id", "first_name", "last_name", "phone", "address")} | {
                "has_usual": bool(c["usual"])}
    raise ApiError(404, "NOT_FOUND", "No customer with that phone number.")


@app.post("/customers", status_code=201, dependencies=[Depends(require_key)], tags=["customers"],
          operation_id="create_customer")
def create_customer(body: NewCustomer):
    p = norm_phone(body.phone)
    if any(c["phone"] == p for c in CUSTOMERS.values()):
        raise ApiError(409, "INVALID_INPUT", "A customer with that phone number already exists.")
    COUNTERS["customer"] += 1
    cid = f"CUST-{COUNTERS['customer']}"
    CUSTOMERS[cid] = {"customer_id": cid, "first_name": body.first_name, "last_name": body.last_name,
                      "phone": p, "address": body.address.model_dump(), "usual": []}
    return {"customer_id": cid, "delivers_to_zip": body.address.zip in SERVICE_ZIPS}


@app.get("/catalog/search", dependencies=[Depends(require_key)], tags=["catalog"], operation_id="search_catalog")
def search_catalog(query: str = Query(..., min_length=1, examples=["oat milk"]),
                   limit: int = Query(3, ge=1, le=10)):
    stem = lambda w: w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
    tokens = [stem(t) for t in re.findall(r"[a-z0-9%']+", query.lower())]
    tokens = [t for t in tokens if t not in {"a", "an", "the", "of", "some", "please", "and", "pack", "lb", "pound"}]
    scored = []
    for p in CATALOG.values():
        words = {stem(w) for w in re.findall(r"[a-z0-9%']+", f"{p['name']} {p['brand']} {' '.join(p['tags'])}".lower())}
        hits = sum(1 for t in tokens if t in words)
        if hits:
            scored.append((hits, p["in_stock"], -p["price"], p))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    best = scored[0][0] if scored else 0
    items = [product_view(s[3]) for s in scored if s[0] == best][:limit]
    exact = bool(items) and best == len(tokens)
    return {"query": query, "count": len(items), "exact_match": exact,
            "note": None if exact or not items else
            "No product matches every word of the request; these are only partial matches.",
            "items": items}


@app.get("/customers/{customer_id}/usual", dependencies=[Depends(require_key)], tags=["customers"],
         operation_id="get_usual_items")
def get_usual_items(customer_id: str):
    c = get_customer(customer_id)
    items = [{**product_view(CATALOG[u["sku"]]), "qty": u["qty"]} for u in c["usual"]]
    return {"customer_id": customer_id, "count": len(items), "items": items}


@app.get("/customers/{customer_id}/orders/last", dependencies=[Depends(require_key)], tags=["orders"],
         operation_id="get_last_order")
def get_last_order(customer_id: str):
    get_customer(customer_id)
    mine = [o for o in ORDERS.values() if o["customer_id"] == customer_id]
    if not mine:
        raise ApiError(404, "NOT_FOUND", "This customer has no previous orders.")
    last = max(mine, key=lambda o: o["created_at"])
    view = order_view(last)
    view["items"] = [{**l, "in_stock": CATALOG[l["sku"]]["in_stock"],
                      "substitute": product_view(CATALOG[l["sku"]])["substitute"]} for l in last["items"]]
    return view


@app.get("/slots", dependencies=[Depends(require_key)], tags=["delivery"], operation_id="get_delivery_slots")
def get_delivery_slots(zip: str = Query(..., pattern=r"^\d{5}$", examples=["10001"]),
                       days: int = Query(SLOT_DAYS, ge=1, le=SLOT_DAYS)):
    if zip not in SERVICE_ZIPS:
        return {"zip": zip, "delivers": False, "slots": [],
                "message": "FreshMart does not deliver to this ZIP code yet."}
    full, today, slots = full_slots(), now().date(), []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        for w in WINDOWS:
            s = slot_obj(d, w)
            s.update({"available": s["slot_id"] not in full, "fee": DELIVERY_FEE,
                      "fee_note": f"Free on orders of ${FREE_DELIVERY_AT:.2f} or more",
                      "cutoff_at": iso(cutoff_for(d))})
            slots.append(s)
    return {"zip": zip, "delivers": True, "slots": slots}


@app.post("/orders", status_code=201, dependencies=[Depends(require_key)], tags=["orders"],
          operation_id="place_order")
def place_order(body: NewOrder):
    c = get_customer(body.customer_id)
    items = check_items(body.items)
    sub = totals(build_lines(items))["subtotal"]
    if sub < MIN_ORDER:
        raise ApiError(422, "BELOW_MINIMUM", f"Minimum order is ${MIN_ORDER:.2f}.",
                       {"subtotal": sub, "shortfall": money(MIN_ORDER - sub)})
    d, w = check_slot(body.slot_id, c["address"]["zip"])
    COUNTERS["order"] += 1
    oid = f"FM-{COUNTERS['order']}"
    o = make_order(oid, c["customer_id"], items, d, w, "placed", body.substitution_pref, body.payment_method)
    if body.payment_method == "pay_link":
        o["pay_link"] = f"https://pay.freshmart.example/{oid}"
    c["usual"] = c["usual"] or [{"sku": i["sku"], "qty": i["qty"]} for i in items]
    return order_view(o)


@app.get("/orders/{order_id}", dependencies=[Depends(require_key)], tags=["orders"], operation_id="get_order")
def get_order(order_id: str, customer_id: str = Query(...)):
    return order_view(get_owned_order(order_id, customer_id))


@app.patch("/orders/{order_id}", dependencies=[Depends(require_key)], tags=["orders"], operation_id="update_order")
def update_order(order_id: str, body: OrderUpdate):
    o = get_owned_order(order_id, body.customer_id)
    if not order_view(o)["can_modify"]:
        raise ApiError(409, "PAST_CUTOFF", "This order can no longer be changed.", {"cutoff_at": o["cutoff_at"]})
    if body.items is None and body.slot_id is None:
        raise ApiError(422, "INVALID_INPUT", "Send items and/or slot_id to change.")
    items = check_items(body.items) if body.items is not None else [{"sku": l["sku"], "qty": l["qty"]} for l in o["items"]]
    lines = build_lines(items)
    t = totals(lines)
    if t["subtotal"] < MIN_ORDER:
        raise ApiError(422, "BELOW_MINIMUM", f"Minimum order is ${MIN_ORDER:.2f}.",
                       {"subtotal": t["subtotal"], "shortfall": money(MIN_ORDER - t["subtotal"])})
    if body.slot_id:
        d, w = check_slot(body.slot_id, CUSTOMERS[o["customer_id"]]["address"]["zip"])
        o["slot"], o["cutoff_at"] = slot_obj(d, w), iso(cutoff_for(d))
    o["items"] = lines
    o.update(t)
    return order_view(o)


@app.post("/orders/{order_id}/cancel", dependencies=[Depends(require_key)], tags=["orders"],
          operation_id="cancel_order")
def cancel_order(order_id: str, body: CancelRequest):
    o = get_owned_order(order_id, body.customer_id)
    if o["status"] == "cancelled":
        return order_view(o)
    if not order_view(o)["can_modify"]:
        raise ApiError(409, "PAST_CUTOFF", "This order can no longer be cancelled.", {"cutoff_at": o["cutoff_at"]})
    o["status"], o["cancel_reason"] = "cancelled", body.reason
    return order_view(o)


@app.post("/complaints", status_code=201, dependencies=[Depends(require_key)], tags=["complaints"],
          operation_id="create_complaint")
def create_complaint(body: NewComplaint):
    o = get_owned_order(body.order_id, body.customer_id)
    if o["status"] != "delivered":
        raise ApiError(422, "INVALID_INPUT", "Complaints can only be filed for delivered orders.")
    line = next((l for l in o["items"] if l["sku"] == body.sku.upper()), None)
    if not line:
        raise ApiError(404, "NOT_FOUND", f"{body.sku} is not part of order {o['order_id']}.")
    qty = min(body.qty, line["qty"])
    value = money(line["unit_price"] * qty)
    recent = [c for c in COMPLAINTS.values() if c["customer_id"] == body.customer_id
              and datetime.fromisoformat(c["created_at"]) > now() - timedelta(days=30)]
    needs_human = value > AUTO_CREDIT_LIMIT or bool(recent)
    reason = ("credit exceeds automatic limit" if value > AUTO_CREDIT_LIMIT
              else "second complaint within 30 days" if recent else None)
    COUNTERS["complaint"] += 1
    cid = f"CMP-{COUNTERS['complaint']}"
    COMPLAINTS[cid] = {
        "complaint_id": cid, "order_id": o["order_id"], "customer_id": body.customer_id, "sku": line["sku"],
        "issue": body.issue, "qty": qty, "credit_amount": 0.0 if needs_human else value,
        "status": "escalated" if needs_human else "credited", "created_at": iso(now()),
    }
    return COMPLAINTS[cid] | {"needs_human": needs_human, "escalation_reason": reason, "item_value": value}


@app.post("/admin/reset", dependencies=[Depends(require_key)], tags=["meta"], operation_id="reset_data")
def reset_data():
    seed()
    return {"status": "reset", "customers": len(CUSTOMERS), "orders": len(ORDERS), "products": len(CATALOG)}
