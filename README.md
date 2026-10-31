# FreshMart Mock API

A mock grocery-ordering backend for the FreshMart Sierra agent (Ava). The data is fake and held in memory. Restarting the service, or calling `POST /admin/reset`, puts everything back to the seed state.

- 80 products in 8 aisles (`seed/catalog.json`)
- 4 seeded customers plus a "new customer" phone number (`seed/customers.json`)
- 4 seeded orders and 1 past complaint, dated relative to today
- Delivery slots: 2-hour windows from 8 am to 8 pm for the next 3 days. ZIPs 10001, 10002, 10003 and 11201 only.
- All money is in USD. All times are America/New_York.

Interactive docs are at `/docs`. The OpenAPI spec is at `/openapi.json`.

---

## 1. Deploy to Render (about 10 minutes, no terminal needed)

1. **Create a GitHub repo.** Go to github.com → New repository → name it `freshmart-mock-api` → choose **Private** → Create.
2. **Upload the files.** In the new repo, click **uploading an existing file**. Drag in everything from this folder, including the `seed` folder, then commit. The repo root must contain `main.py`, `requirements.txt` and `render.yaml`.
3. **Create the service.** Go to render.com → sign in with GitHub → **New → Blueprint** → pick the `freshmart-mock-api` repo → **Apply**. Render reads `render.yaml`, installs the dependencies and starts the API on the free plan.
4. **Copy two values.**
   - **URL:** shown at the top of the service page, e.g. `https://freshmart-mock-api-xxxx.onrender.com`.
   - **API key:** service → **Environment** → `FRESHMART_API_KEY`. Render generates this value; reveal it and copy it.
5. **Check it.**
   - Open `<URL>/`. It should return `"status": "ok"`.
   - Open `<URL>/docs`, click **Authorize**, paste the key, then use **Try it out** on any endpoint (e.g. search for `milk`).

**Free-plan caveat:** the service sleeps after about 15 minutes with no traffic, and the first request after that takes 30–60 seconds. Open `<URL>/` in a browser before a test session or demo so Sierra's tool calls don't time out.

## 2. Connect to Sierra

1. **Environments → Test.** Add a variable `FRESHMART_API_BASE_URL` set to the Render URL. Add a **secret** `FRESHMART_API_KEY` set to the key.
2. **Integrations → Custom API.**
   - Base URL: the environment variable.
   - Auth: the request header `X-API-Key` set to the secret.
   - If it asks for an OpenAPI spec, give it `<URL>/openapi.json`.
3. **Library → Tools.** Create one tool per endpoint, using the names below. The `operationId` in the OpenAPI spec already matches each tool name in the spec doc.

| Sierra tool | Method and path |
| --- | --- |
| `lookup_customer` | GET `/customers/by-phone?phone=` |
| `create_customer` | POST `/customers` |
| `search_catalog` | GET `/catalog/search?query=&limit=` |
| `get_usual_items` | GET `/customers/{customer_id}/usual` |
| `get_last_order` | GET `/customers/{customer_id}/orders/last` |
| `get_delivery_slots` | GET `/slots?zip=` |
| `place_order` | POST `/orders` |
| `get_order` | GET `/orders/{order_id}?customer_id=` |
| `update_order` | PATCH `/orders/{order_id}` |
| `cancel_order` | POST `/orders/{order_id}/cancel` |
| `create_complaint` | POST `/complaints` |

`POST /admin/reset` is for you, not the agent. Call it from `/docs` between test runs.

## 3. Behaviour the agent must handle

**Errors.** Every error looks like `{"error": {"code", "message", "details?"}}`.

| Code | HTTP | When | What Ava should do |
| --- | --- | --- | --- |
| `NOT_FOUND` | 404 | Unknown phone, customer, order or SKU; or the order belongs to someone else | Phone not found: start new-customer signup. Anything else: ask the customer to check the detail. |
| `OUT_OF_STOCK` | 409 | `place_order` or `update_order` includes an out-of-stock SKU (`details.skus`) | Offer the substitute or remove the item. |
| `SLOT_FULL` | 409 | The window is full (tomorrow 4–6 pm, always) | Offer the other windows. |
| `PAST_CUTOFF` | 409 | Change or cancel after 10 pm the day before delivery | Explain; transfer if the customer insists. |
| `BELOW_MINIMUM` | 422 | Subtotal under $35 (`details.shortfall`) | Say how much more is needed. |
| `INVALID_INPUT` | 422 | Bad field; age-restricted item; ZIP out of area; fractional count on an "each" item | Explain plainly. |
| `UNAUTHORIZED` | 401 | Missing or wrong API key | Configuration problem, not the customer's. |

**Search notes**

- Results are ranked by how many words match, in-stock items first.
- `exact_match: false` means nothing matched every word. Example: "dragon fruit" returns apples. Ava must say FreshMart doesn't carry the item, not offer the partial matches as if they were it.
- Out-of-stock items include a `substitute` object when one exists.
- Wine and beer come back with `age_restricted: true`, and ordering them is refused.

**Money rules**

- Minimum order: $35.00 subtotal.
- Delivery fee: $5.99, free when the subtotal is $100.00 or more.
- Tax: 8.875%, on Household items only (groceries are untaxed).
- `lb` items accept decimal quantities (2.5 = 2.5 lb). `each` items need whole numbers.

**Complaints**

- Credit is unit price × quantity.
- `needs_human: true` (with `credit_amount: 0`) when the credit would be over $25, or when the customer already has a complaint in the last 30 days.

## 4. Test data cheat sheet

| Phone | Customer | ZIP | Seeded order | Use it to test |
| --- | --- | --- | --- | --- |
| +1 212 555 0101 | Sarah Miller (`CUST-1001`) | 10001 | `FM-10421`: placed, delivery in 2 days 2–4 pm, changeable | New order, reorder usual (9 items, Oatly out of stock), modify, cancel |
| +1 212 555 0102 | James Carter (`CUST-1002`) | 10002 | `FM-10388`: delivered yesterday | Complaint under $25 (e.g. 4 missing yogurts = $5.96) |
| +1 212 555 0103 | Priya Shah (`CUST-1003`) | 11201 | `FM-10350`: out for delivery today | Status; change after cutoff → `PAST_CUTOFF` |
| +1 212 555 0104 | Tom Nguyen (`CUST-1004`) | 10003 | `FM-10301`: delivered, complaint `CMP-5001` filed 3 days ago | Second complaint → `needs_human` |
| +1 212 555 0199 | (none) | — | — | New customer signup |

**Products that test edge cases**

- `DAI-003` Oatly: out of stock, substitute is `DAI-004` Silk
- `BEV-001` Tropicana: out of stock, no substitute
- `PRO-010` strawberries, `BAK-009` muffins: out of stock, no substitute
- `BEV-009` wine, `BEV-010` beer: age-restricted
- "milk" returns whole and 2%, so Ava has to ask which one
- `PRO-001` bananas, `MEA-003` chicken: priced per lb

## 5. Run locally (optional)

```bash
pip install -r requirements.txt pytest httpx
uvicorn main:app --reload        # http://127.0.0.1:8000/docs, key: freshmart-dev-key
python -m pytest -q              # 16 tests covering the spec scenarios
```
