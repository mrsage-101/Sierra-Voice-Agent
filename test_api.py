"""Smoke tests covering the Phase 1 spec scenarios. Run: python -m pytest -q"""

from datetime import timedelta

from fastapi.testclient import TestClient

import main

c = TestClient(main.app)
H = {"X-API-Key": main.API_KEY}


def setup_function():
    c.post("/admin/reset", headers=H)


def sid(days, start):
    return main.slot_id(main.now().date() + timedelta(days=days), start)


def test_auth_required():
    r = c.get("/catalog/search", params={"query": "milk"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_lookup_phone_formats():
    for p in ["+12125550101", "2125550101", "(212) 555-0101"]:
        r = c.get("/customers/by-phone", params={"phone": p}, headers=H)
        assert r.json()["first_name"] == "Sarah"
    r = c.get("/customers/by-phone", params={"phone": "+12125550199"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_milk_is_ambiguous():  # T2
    items = c.get("/catalog/search", params={"query": "milk"}, headers=H).json()["items"]
    names = [i["name"] for i in items]
    assert "Whole Milk" in names and "2% Reduced Fat Milk" in names


def test_oat_milk_substitute():  # T3
    items = c.get("/catalog/search", params={"query": "Oatly oat milk"}, headers=H).json()["items"]
    oatly = items[0]
    assert oatly["sku"] == "DAI-003" and not oatly["in_stock"] and oatly["substitute"]["sku"] == "DAI-004"


def test_oj_no_substitute_and_wine_restricted():  # T4, T12
    oj = c.get("/catalog/search", params={"query": "orange juice"}, headers=H).json()["items"][0]
    assert not oj["in_stock"] and oj["substitute"] is None
    wine = c.get("/catalog/search", params={"query": "red wine"}, headers=H).json()["items"][0]
    assert wine["age_restricted"]


def test_unknown_product():  # T13
    r = c.get("/catalog/search", params={"query": "dragon fruit"}, headers=H).json()
    assert r["exact_match"] is False
    assert c.get("/catalog/search", params={"query": "xyzzy"}, headers=H).json()["count"] == 0
    assert c.get("/catalog/search", params={"query": "bananas"}, headers=H).json()["exact_match"] is True


def test_place_order_happy_path():  # T1
    body = {"customer_id": "CUST-1001", "items": [
        {"sku": "DAI-001", "qty": 1}, {"sku": "PRO-001", "qty": 2}, {"sku": "MEA-003", "qty": 2.5},
        {"sku": "BAK-002", "qty": 1}, {"sku": "HOU-001", "qty": 1}],
        "slot_id": sid(1, 14), "substitution_pref": "substitute", "payment_method": "pay_link"}
    o = c.post("/orders", json=body, headers=H).json()
    assert o["order_id"].startswith("FM-") and o["status"] == "placed"
    assert abs(o["subtotal"] - 39.13) < 0.01
    assert o["delivery_fee"] == 5.99 and o["tax"] == round(11.99 * 0.08875, 2)
    assert o["pay_link"] and o["can_modify"]


def test_below_minimum():  # T7
    body = {"customer_id": "CUST-1002", "items": [{"sku": "PAN-008", "qty": 2}, {"sku": "DAI-001", "qty": 1}],
            "slot_id": sid(1, 10), "substitution_pref": "skip", "payment_method": "card_on_delivery"}
    e = c.post("/orders", json=body, headers=H).json()["error"]
    assert e["code"] == "BELOW_MINIMUM" and e["details"]["shortfall"] == round(35 - 24.27, 2)


def test_free_delivery_over_100():  # T8
    body = {"customer_id": "CUST-1003", "items": [{"sku": "MEA-005", "qty": 9}],
            "slot_id": sid(2, 10), "substitution_pref": "skip", "payment_method": "card_on_delivery"}
    assert c.post("/orders", json=body, headers=H).json()["delivery_fee"] == 0.0


def test_full_slot_and_out_of_area():  # T9, T10
    body = {"customer_id": "CUST-1001", "items": [{"sku": "MEA-005", "qty": 4}],
            "slot_id": sid(1, 16), "substitution_pref": "skip", "payment_method": "card_on_delivery"}
    assert c.post("/orders", json=body, headers=H).json()["error"]["code"] == "SLOT_FULL"
    slots = c.get("/slots", params={"zip": "10001"}, headers=H).json()["slots"]
    assert len(slots) == 18 and sum(not s["available"] for s in slots) == 1
    assert c.get("/slots", params={"zip": "90210"}, headers=H).json()["delivers"] is False


def test_out_of_stock_rejected():
    body = {"customer_id": "CUST-1001", "items": [{"sku": "DAI-003", "qty": 9}],
            "slot_id": sid(1, 10), "substitution_pref": "skip", "payment_method": "card_on_delivery"}
    e = c.post("/orders", json=body, headers=H).json()["error"]
    assert e["code"] == "OUT_OF_STOCK" and e["details"]["skus"] == ["DAI-003"]


def test_reorder_usual():  # T6
    usual = c.get("/customers/CUST-1001/usual", headers=H).json()
    assert usual["count"] == 9 and any(not i["in_stock"] for i in usual["items"])
    last = c.get("/customers/CUST-1001/orders/last", headers=H).json()
    assert last["order_id"] == "FM-10421"


def test_status_and_modify():  # T14, T15, T16
    o = c.get("/orders/FM-10350", params={"customer_id": "CUST-1003"}, headers=H).json()
    assert o["status"] == "out_for_delivery" and not o["can_modify"]
    r = c.patch("/orders/FM-10350", json={"customer_id": "CUST-1003", "slot_id": sid(2, 10)}, headers=H)
    assert r.json()["error"]["code"] == "PAST_CUTOFF"
    r = c.patch("/orders/FM-10421", json={"customer_id": "CUST-1001", "items": [
        {"sku": "MEA-005", "qty": 3}, {"sku": "DAI-001", "qty": 1}]}, headers=H).json()
    assert r["subtotal"] == round(3 * 11.99 + 4.29, 2)
    assert c.get("/orders/FM-10421", params={"customer_id": "CUST-1002"}, headers=H).status_code == 404


def test_cancel():
    r = c.post("/orders/FM-10421/cancel", json={"customer_id": "CUST-1001"}, headers=H).json()
    assert r["status"] == "cancelled"


def test_complaints():  # T17, T18
    r = c.post("/complaints", json={"customer_id": "CUST-1002", "order_id": "FM-10388", "sku": "DAI-006",
                                    "issue": "missing", "qty": 4}, headers=H).json()
    assert r["credit_amount"] == 5.96 and not r["needs_human"]
    r = c.post("/complaints", json={"customer_id": "CUST-1004", "order_id": "FM-10301", "sku": "PRO-006",
                                    "issue": "damaged", "qty": 1}, headers=H).json()
    assert r["needs_human"] and r["escalation_reason"] == "second complaint within 30 days"


def test_new_customer_flow():  # T19
    r = c.post("/customers", json={"first_name": "Alex", "last_name": "Kim", "phone": "+12125550199",
               "address": {"line1": "1 Test Plaza", "city": "New York", "state": "NY", "zip": "10001"}},
               headers=H).json()
    assert r["customer_id"].startswith("CUST-") and r["delivers_to_zip"]
