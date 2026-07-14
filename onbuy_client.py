"""OnBuy REST API client.

Wraps the auth / create-product / update-listing calls that previously only
existed as disconnected manual test scripts (test_onbuy_auth.py,
test_create_product.py, test_update_listing.py) so the scheduled pipeline can
actually push to OnBuy's API instead of relying solely on the XML feed
importer. The XML feed generation stays in generate_xml.py as a fallback.
"""
import logging
import os

import requests

from retry_utils import AuthError, PermanentError, raise_for_status, with_retry

logger = logging.getLogger("onbuy_sync")

BASE_URL = "https://api.onbuy.com/v2"


def _raise_on_result_error(body, sku, what):
    """OnBuy's bulk-style endpoints (e.g. PUT /v2/listings/by-sku) return HTTP
    200 with a top-level "success": true even when the actual per-item
    operation failed - the real outcome is buried in body["results"][i]["error"].
    A plain HTTP-status check (raise_for_status) cannot see this at all, which
    is exactly how every push in earlier runs was silently failing with
    "SKU does not exist" while being logged as a success. Check explicitly.
    """
    if not isinstance(body, dict):
        return
    results = body.get("results")
    if not isinstance(results, list) or not results:
        return
    matching = [r for r in results if isinstance(r, dict) and r.get("sku") == sku]
    target = matching[0] if matching else (results[0] if len(results) == 1 else None)
    if isinstance(target, dict) and target.get("error"):
        raise PermanentError(f"{what}: {target['error']}")


class OnBuyClient:
    def __init__(self, consumer_key=None, secret_key=None, seller_id=None, site_id=None, use_sandbox=None):
        # OnBuy's sandbox is the same api.onbuy.com host - only the credentials
        # differ, so this is a credential swap, not a base-URL swap.
        if use_sandbox is None:
            use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
        self.use_sandbox = use_sandbox

        if use_sandbox:
            self.consumer_key = consumer_key or os.getenv("ONBUY_TEST_CONSUMER_KEY")
            self.secret_key = secret_key or os.getenv("ONBUY_TEST_SECRET_KEY")
            # Sandbox seller/site IDs haven't been provided separately yet -
            # fall back to the production ones. If OnBuy issues distinct
            # sandbox IDs later, set ONBUY_TEST_SELLER_ID/ONBUY_TEST_SITE_ID
            # and this will pick them up automatically.
            self.seller_id = int(seller_id or os.getenv("ONBUY_TEST_SELLER_ID") or os.getenv("ONBUY_SELLER_ID") or 0)
            self.site_id = int(site_id or os.getenv("ONBUY_TEST_SITE_ID") or os.getenv("ONBUY_SITE_ID") or 0)
        else:
            self.consumer_key = consumer_key or os.getenv("ONBUY_CONSUMER_KEY")
            self.secret_key = secret_key or os.getenv("ONBUY_SECRET_KEY")
            self.seller_id = int(seller_id or os.getenv("ONBUY_SELLER_ID") or 0)
            self.site_id = int(site_id or os.getenv("ONBUY_SITE_ID") or 0)

        self._token = None
        logger.info("OnBuyClient initialized (sandbox=%s)", self.use_sandbox)

    def authenticate(self):
        """Returns True on success. Never raises - callers must check the
        return value before doing per-item work. The old pipeline used a
        possibly-None eBay token as if it were valid, letting a bad token
        cascade through an entire batch; this client refuses to do that."""
        if not self.consumer_key or not self.secret_key:
            key_names = "ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY" if self.use_sandbox else "ONBUY_CONSUMER_KEY/ONBUY_SECRET_KEY"
            logger.error("OnBuy credentials missing (%s)", key_names)
            return False

        def _do_auth():
            resp = requests.post(
                f"{BASE_URL}/auth/request-token",
                data={"consumer_key": self.consumer_key, "secret_key": self.secret_key},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            raise_for_status(resp, what="onbuy auth")
            token = resp.json().get("access_token")
            if not token:
                raise AuthError("onbuy auth response missing access_token")
            return token

        try:
            self._token = with_retry(_do_auth, what="onbuy auth", max_attempts=3)
        except (AuthError, PermanentError) as exc:
            logger.error("OnBuy authentication failed: %s", exc)
            self._token = None
        except Exception as exc:
            logger.error("OnBuy authentication failed after retries: %s", exc)
            self._token = None
        return bool(self._token)

    def health_check(self):
        return bool(self._token) or self.authenticate()

    def _headers(self):
        # OnBuy expects the raw token as the Authorization value, not "Bearer <token>"
        # - matches the already-verified test_create_product.py/test_update_listing.py behavior.
        return {"Authorization": self._token, "Content-Type": "application/json"}

    def _send(self, method, url, *, what, **kwargs):
        """requests.request with one automatic re-auth on a 401. The token is
        fetched once at run start, but it can die mid-run: OnBuy tokens
        expire, and any other workflow requesting a token with the same
        credentials (e.g. the hourly backfill firing while a long sync run
        is still going) can supersede it - confirmed 2026-07-06 when a run
        overlapping another one started getting 401 "badToken" on every call
        partway through. Refresh once and repeat the call instead of failing
        every remaining push in the run.
        """
        kwargs.setdefault("timeout", 60)
        resp = requests.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401 and self.authenticate():
            logger.info("%s: OnBuy token rejected mid-run - re-authenticated, retrying once", what)
            resp = requests.request(method, url, headers=self._headers(), **kwargs)
        return resp

    def create_product(self, *, sku, ean, title, description, brand, category_id, price, main_image, additional_images):
        payload = {
            "site_id": self.site_id,
            "seller_id": self.seller_id,
            "uid": sku,
            "published": "1",
            "category_id": category_id,
            "product_codes": [ean] if ean else [],
            "rrp": str(price),
            "product_name": title[:150],
            "brand_name": brand or "Unbranded",
            # OnBuy nulls an empty-string description server-side and then
            # rejects the whole create with 400 "description must be a
            # string" (seen 2026-07-06 for eBay listings that genuinely have
            # no description text). Fall back to the title so those products
            # can still be listed; the Sheet/Supabase keep the real (empty)
            # description.
            "description": (description or "").strip() or title[:150],
            "default_image": main_image,
            "additional_images": additional_images[:10],
            "force_update": True,
        }

        def _do_create():
            logger.info("OnBuy create_product(%s) request payload: %s", sku, payload)
            resp = self._send("POST", f"{BASE_URL}/products", what=f"onbuy create_product({sku})", json=payload)
            logger.info("OnBuy create_product(%s) raw response [%s]: %s", sku, resp.status_code, resp.text[:2000])
            raise_for_status(resp, what=f"onbuy create_product({sku})")
            body = resp.json()
            _raise_on_result_error(body, sku, what=f"onbuy create_product({sku})")
            return body

        return with_retry(_do_create, what=f"onbuy create_product({sku})", max_attempts=3)

    def update_listing(self, *, sku, price, stock):
        payload = {
            "site_id": self.site_id,
            "seller_id": self.seller_id,
            "listings": [{"sku": sku, "price": price, "stock": stock, "boost_marketing_commission": 0}],
        }

        def _do_update():
            logger.info("OnBuy update_listing(%s) request payload: %s", sku, payload)
            resp = self._send("PUT", f"{BASE_URL}/listings/by-sku", what=f"onbuy update_listing({sku})", json=payload)
            logger.info("OnBuy update_listing(%s) raw response [%s]: %s", sku, resp.status_code, resp.text[:2000])
            raise_for_status(resp, what=f"onbuy update_listing({sku})")
            body = resp.json()
            _raise_on_result_error(body, sku, what=f"onbuy update_listing({sku})")
            return body

        return with_retry(_do_update, what=f"onbuy update_listing({sku})", max_attempts=3)

    def list_listings(self):
        """GET /v2/listings - the only direct way to see what's actually in
        this account's catalog via the API. Needed because OnBuy's seller
        dashboard only ever shows the production catalog, even when
        authenticated with sandbox credentials - there is no visible UI for
        sandbox data, so this is the sole ground truth for sandbox testing.
        """
        def _do_list():
            resp = self._send(
                "GET", f"{BASE_URL}/listings", what="onbuy list_listings",
                params={"site_id": self.site_id}, timeout=30,
            )
            logger.info("OnBuy list_listings raw response [%s]: %s", resp.status_code, resp.text[:3000])
            raise_for_status(resp, what="onbuy list_listings")
            return resp.json()

        return with_retry(_do_list, what="onbuy list_listings", max_attempts=3)

    def check_queue(self, queue_id):
        """GET /v2/queues?queue_id=... - per OnBuy support (2026-07-02): a
        queue_id from POST /v2/products only means "accepted for async
        processing," not "created." This checks whether that submission has
        succeeded, is still pending, or failed - and, unlike the create
        response itself, surfaces the actual validation/processing error.

        Passed as a query param, not a URL path segment - putting it in the
        path (/v2/queues/{queue_id}) got back "'' is not a valid queue_id",
        meaning OnBuy's router wasn't reading it from the path at all.
        """
        def _do_check():
            resp = self._send(
                "GET", f"{BASE_URL}/queues", what=f"onbuy check_queue({queue_id})",
                params={"site_id": self.site_id, "queue_id": queue_id}, timeout=30,
            )
            logger.info("OnBuy check_queue(%s) raw response [%s]: %s", queue_id, resp.status_code, resp.text[:3000])
            raise_for_status(resp, what=f"onbuy check_queue({queue_id})")
            return resp.json()

        return with_retry(_do_check, what=f"onbuy check_queue({queue_id})", max_attempts=3)

    def list_queue(self, limit=50, offset=0):
        """GET /v2/queues, paginated - in practice the queue_id param above
        didn't filter results (every queue_id tested returned the same recent
        history), so this is the more reliable way to scan for a specific
        SKU's outcome: page through recent submissions and match by "uid"
        (which is the SKU). Used by backfill_onbuy_status.py.
        """
        def _do_list():
            resp = self._send(
                "GET", f"{BASE_URL}/queues", what=f"onbuy list_queue(offset={offset})",
                params={"site_id": self.site_id, "limit": limit, "offset": offset}, timeout=30,
            )
            raise_for_status(resp, what=f"onbuy list_queue(offset={offset})")
            return resp.json()

        return with_retry(_do_list, what=f"onbuy list_queue(offset={offset})", max_attempts=3)

    def sync_product(self, **kwargs):
        """Update price/stock for an existing SKU; if OnBuy reports the SKU
        doesn't exist ("SKU does not exist", returned as HTTP 200 with the
        real error buried in results[].error - see _raise_on_result_error),
        create it instead.
        """
        sku, price, stock = kwargs["sku"], kwargs["price"], kwargs["stock"]
        try:
            return "updated", self.update_listing(sku=sku, price=price, stock=stock)
        except PermanentError as exc:
            logger.info("update_listing(%s) rejected (%s) - attempting create_product instead", sku, exc)
            result = self.create_product(
                sku=sku,
                ean=kwargs.get("ean"),
                title=kwargs["title"],
                description=kwargs["description"],
                brand=kwargs.get("brand"),
                category_id=kwargs["category_id"],
                price=price,
                main_image=kwargs.get("main_image", ""),
                additional_images=kwargs.get("additional_images", []),
            )
            return "created", result
