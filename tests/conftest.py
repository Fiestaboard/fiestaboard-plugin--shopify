"""Shared fixtures for the Shopify plugin tests.

``shopify_api`` patches ``requests.post`` with a fake Shopify that answers the
token endpoint and the GraphQL Admin API with payloads shaped like the real
ones. Tests adjust its state (orders, shop, low-stock variants) or queue raw
responses to simulate errors.
"""

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from plugins.shopify import ShopifyPlugin

MANIFEST_PATH = Path(__file__).parent.parent / "manifest.json"
SHOP_DOMAIN = "test-store.myshopify.com"

# 2026-09-30 14:00 in New York (EDT, UTC-4); a Wednesday.
NOW = datetime.fromisoformat("2026-09-30T18:00:00+00:00")


class FakeResponse:
    def __init__(self, body=None, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")


def make_order(number, created_at, amount, test=False, cancelled=False):
    return {
        "id": f"gid://shopify/Order/{number}",
        "name": f"#{number}",
        "createdAt": created_at,
        "test": test,
        "cancelledAt": "2026-09-30T17:00:00Z" if cancelled else None,
        "currentTotalPriceSet": {"shopMoney": {"amount": str(amount), "currencyCode": "USD"}},
    }


def make_variant(product, title, quantity, tracked=True, status="ACTIVE"):
    return {
        "id": f"gid://shopify/ProductVariant/{abs(hash((product, title))) % 10**8}",
        "title": title,
        "inventoryQuantity": quantity,
        "inventoryItem": {"tracked": tracked},
        "product": {"title": product, "status": status},
    }


class FakeShopify:
    """Answers requests.post the way Shopify would."""

    def __init__(self):
        self.shop = {"name": "My Test Store", "currencyCode": "USD", "ianaTimezone": "America/New_York"}
        self.latest_orders = [
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
            make_order(1041, "2026-09-30T15:10:00Z", "40.50"),
        ]
        # Each page is a list of orders; pages are chained with cursors.
        self.period_pages = [[
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
            make_order(1041, "2026-09-30T15:10:00Z", "40.50"),
        ]]
        self.low_stock = []
        self.low_stock_has_more = False
        self.token_status = 200
        self.queued = deque()
        self.calls = []
        self.token_requests = 0
        self.graphql_requests = []

    # Responses that override the next GraphQL call(s), e.g. errors.
    def queue(self, body=None, status_code=200, headers=None):
        self.queued.append(FakeResponse(body, status_code, headers))

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/admin/oauth/access_token"):
            self.token_requests += 1
            return FakeResponse(
                {"access_token": f"test_token_{self.token_requests}", "scope": "read_orders,read_products",
                 "expires_in": 86399},
                self.token_status,
            )

        self.graphql_requests.append(kwargs)
        if self.queued:
            return self.queued.popleft()

        query = kwargs["json"]["query"]
        variables = kwargs["json"]["variables"]
        if "FiestaBoardOverview" in query:
            data = {
                "shop": self.shop,
                "latestOrders": {"nodes": self.latest_orders[: variables["latestFirst"]]},
            }
            if variables["includeLowStock"]:
                data["lowStock"] = {
                    "nodes": self.low_stock,
                    "pageInfo": {"hasNextPage": self.low_stock_has_more},
                }
        elif "FiestaBoardPeriodOrders" in query:
            index = int(variables["after"] or 0)
            has_next = index + 1 < len(self.period_pages)
            data = {"orders": {
                "nodes": self.period_pages[index],
                "pageInfo": {"hasNextPage": has_next, "endCursor": str(index + 1) if has_next else None},
            }}
        else:  # pragma: no cover - a test sent an unknown query
            raise AssertionError(f"Unexpected query: {query}")
        return FakeResponse({"data": data, "extensions": {"cost": {"requestedQueryCost": 30}}})


@pytest.fixture(autouse=True)
def clear_shopify_env(monkeypatch):
    """Keep a developer's real SHOPIFY_* variables out of the tests."""
    for name in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def fixed_now():
    with patch("plugins.shopify._utcnow", return_value=NOW):
        yield NOW


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("plugins.shopify.time.sleep") as sleep:
        yield sleep


@pytest.fixture
def manifest_data():
    with open(MANIFEST_PATH) as f:
        return json.load(f)


@pytest.fixture
def shopify_api():
    fake = FakeShopify()
    with patch("plugins.shopify.requests.post", side_effect=fake):
        yield fake


@pytest.fixture
def base_config():
    return {
        "enabled": True,
        "shop_domain": SHOP_DOMAIN,
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
    }


@pytest.fixture
def plugin(manifest_data, base_config):
    p = ShopifyPlugin(manifest_data)
    p.config = dict(base_config)
    return p
