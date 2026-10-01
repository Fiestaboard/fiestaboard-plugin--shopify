"""Shopify plugin for FiestaBoard.

Shows a Shopify store's sales, order count, latest order and low-stock
variants on the board, and can push a "new order" page when an order comes in.

Authentication works without a redirect URI, so it fits a board on a LAN:

* **Client credentials** (Dev Dashboard apps, the only kind Shopify lets
  merchants create since 2026-01-01): the plugin exchanges the app's Client ID
  and Client secret for an Admin API token that lasts 24 hours, and gets a new
  one before it expires.
* **Admin API access token** (``shpat_...``) from a legacy custom app created
  in the Shopify admin before 2026. Those keep working.

Data comes from the GraphQL Admin API, pinned to ``API_VERSION``.
"""

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests

from src.board_chars import BoardChars
from src.plugins.base import PluginBase, PluginResult, TriggerResult
from src.text_to_board import count_tiles, take_tiles

logger = logging.getLogger(__name__)

API_VERSION = "2026-07"
USER_AGENT = "FiestaBoard Shopify Plugin (https://github.com/Fiestaboard/fiestaboard-plugin--shopify)"
REQUEST_TIMEOUT_SECONDS = 15

ENV_SHOP_DOMAIN = "SHOPIFY_SHOP_DOMAIN"
ENV_CLIENT_ID = "SHOPIFY_CLIENT_ID"
ENV_CLIENT_SECRET = "SHOPIFY_CLIENT_SECRET"
ENV_ACCESS_TOKEN = "SHOPIFY_ACCESS_TOKEN"

# Get a new client-credentials token this long before the old one expires.
TOKEN_REFRESH_MARGIN_SECONDS = 300

# 100 orders per page keeps each request's query cost (~300 points) well under
# the per-request limit and close to one second of a Standard plan's restore rate.
ORDERS_PAGE_SIZE = 100
MAX_ORDER_PAGES = 50
LATEST_ORDERS_COUNT = 10
LOW_STOCK_PAGE_SIZE = 100
MAX_THROTTLE_RETRIES = 3
MAX_THROTTLE_WAIT_SECONDS = 5.0
SEEN_ORDER_IDS_LIMIT = 200

DEFAULT_ALERT_SECONDS = 60
NEW_ORDER_TRIGGER_PRIORITY = 5
GREEN_TILE = "{66}"

# Board fallback geometry when no board is bound (Flagship).
_DEFAULT_ROWS = 6
_DEFAULT_COLS = 22

SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

PERIOD_LABELS = {
    "today": "TODAY",
    "week": "THIS WEEK",
    "month": "THIS MONTH",
}

# Currencies written with a plain "$". Everything else is prefixed with its
# ISO code ("EUR 1,284"), since the board has no other currency symbols.
DOLLAR_CURRENCIES = {"USD", "CAD", "AUD", "NZD", "MXN", "SGD", "HKD"}

OVERVIEW_QUERY = """
query FiestaBoardOverview($latestFirst: Int!, $includeLowStock: Boolean!, $lowStockFirst: Int!, $lowStockQuery: String!) {
  shop {
    name
    currencyCode
    ianaTimezone
  }
  latestOrders: orders(first: $latestFirst, sortKey: CREATED_AT, reverse: true) {
    nodes {
      id
      name
      createdAt
      test
      cancelledAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
    }
  }
  lowStock: productVariants(first: $lowStockFirst, query: $lowStockQuery) @include(if: $includeLowStock) {
    nodes {
      id
      title
      inventoryQuantity
      inventoryItem { tracked }
      product { title status }
    }
    pageInfo { hasNextPage }
  }
}
"""

PERIOD_ORDERS_QUERY = """
query FiestaBoardPeriodOrders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, sortKey: CREATED_AT, reverse: true, query: $query) {
    nodes {
      id
      createdAt
      test
      cancelledAt
      currentTotalPriceSet { shopMoney { amount } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ShopifyError(Exception):
    """An error whose message is safe and useful to show to the board owner."""


def _utcnow() -> datetime:
    """Current time as an aware UTC datetime (patched in tests)."""
    return datetime.now(pytz.utc)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def normalize_shop_domain(raw: str) -> str:
    """Turn what a user pastes into a bare ``<store>.myshopify.com`` host.

    Accepts ``https://your-store.myshopify.com/admin``, ``your-store.myshopify.com``
    and the bare handle ``your-store``.
    """
    domain = (raw or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0]
    if domain and "." not in domain:
        domain = f"{domain}.myshopify.com"
    return domain


def board_safe(text: str) -> str:
    """Uppercase *text* and keep only characters the board can show.

    Accents are folded (``Café`` -> ``CAFE``). Emoji and any other character
    without a board code are dropped, including braces, which the board would
    otherwise read as color-tile markers.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    kept = []
    for ch in folded.upper():
        if ch.isspace():
            kept.append(" ")
        elif BoardChars.get_char_code(ch) is not None:
            kept.append(ch)
    return re.sub(r" {2,}", " ", "".join(kept)).strip()


def format_money(amount: Decimal, currency: str) -> str:
    """Format *amount* in whole currency units for the board: ``$1,284`` or ``EUR 1,284``."""
    whole = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sign = "-" if whole < 0 else ""
    number = f"{abs(whole):,}"
    if currency in DOLLAR_CURRENCIES:
        return f"{sign}${number}"
    return f"{sign}{board_safe(currency)} {number}"


def format_ago(then: datetime, now: datetime) -> str:
    """Short relative time: ``JUST NOW``, ``12M AGO``, ``3H AGO``, ``2D AGO``."""
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "JUST NOW"
    if minutes < 60:
        return f"{minutes}M AGO"
    if minutes < 60 * 24:
        return f"{minutes // 60}H AGO"
    return f"{minutes // (60 * 24)}D AGO"


def period_start(now_local: datetime, period: str) -> datetime:
    """Midnight at the start of *period* in the store's timezone.

    Weeks start on Monday. ``now_local`` must be a pytz-aware datetime.
    """
    tz = now_local.tzinfo
    today = now_local.date()
    if period == "week":
        start_day = today - timedelta(days=today.weekday())
    elif period == "month":
        start_day = today.replace(day=1)
    else:
        start_day = today
    naive = datetime(start_day.year, start_day.month, start_day.day)
    # localize() picks the right UTC offset for that date (DST-aware);
    # is_dst=False avoids an exception when midnight doesn't exist that day.
    return tz.localize(naive, is_dst=False)  # type: ignore[union-attr]


def parse_datetime(value: str) -> datetime:
    """Parse Shopify's ISO-8601 timestamps (``2026-09-30T14:03:11Z``)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def order_amount(order: Dict[str, Any]) -> Decimal:
    """The order's current total in the shop currency (after refunds and edits)."""
    money = ((order.get("currentTotalPriceSet") or {}).get("shopMoney") or {})
    return Decimal(str(money.get("amount") or "0"))


def _numeric_id(gid: str) -> str:
    """``gid://shopify/Order/450789469`` -> ``450789469``."""
    return str(gid).rsplit("/", 1)[-1]


def _center(text: str, width: int) -> str:
    """Center *text* in *width* tiles, truncating if needed (tile-aware)."""
    text, _ = take_tiles(text, width)
    pad = width - count_tiles(text)
    left = pad // 2
    return (" " * left) + text + (" " * (pad - left))


def _fit(text: str, width: int = _DEFAULT_COLS) -> str:
    """Truncate *text* to *width* tiles."""
    return take_tiles(text, width)[0]


# ----------------------------------------------------------------------
# Plugin
# ----------------------------------------------------------------------


@dataclass
class _PendingAlert:
    """New orders waiting to be returned once from ``check_triggers()``."""

    orders: List[Dict[str, Any]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


class ShopifyPlugin(PluginBase):
    """Shopify store stats and new-order alerts."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        self._lock = threading.RLock()
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._reset_order_tracking()

    @property
    def plugin_id(self) -> str:
        return "shopify"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _read(config: Dict[str, Any], key: str, env: str) -> str:
        return str(config.get(key) or os.getenv(env, "") or "").strip()

    def _setting(self, key: str, env: str) -> str:
        return self._read(self.config, key, env)

    def _shop_domain(self) -> str:
        return normalize_shop_domain(self._setting("shop_domain", ENV_SHOP_DOMAIN))

    def _uses_static_token(self) -> bool:
        return bool(self._setting("access_token", ENV_ACCESS_TOKEN))

    def _period(self) -> str:
        period = self.config.get("sales_period") or "today"
        return period if period in PERIOD_LABELS else "today"

    def _low_stock_threshold(self) -> int:
        try:
            return max(0, int(self.config.get("low_stock_threshold") or 0))
        except (TypeError, ValueError):
            return 0

    def _include_test_orders(self) -> bool:
        return bool(self.config.get("include_test_orders", False))

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []

        domain = normalize_shop_domain(self._read(config, "shop_domain", ENV_SHOP_DOMAIN))
        if not domain:
            errors.append("Store domain is required, e.g. your-store.myshopify.com")
        elif not SHOP_DOMAIN_RE.match(domain):
            errors.append(
                "Store domain must be your .myshopify.com address, e.g. your-store.myshopify.com "
                "(custom domains don't work with the Admin API)"
            )

        client_id = self._read(config, "client_id", ENV_CLIENT_ID)
        client_secret = self._read(config, "client_secret", ENV_CLIENT_SECRET)
        access_token = self._read(config, "access_token", ENV_ACCESS_TOKEN)
        if bool(client_id) != bool(client_secret):
            errors.append("Client ID and Client secret must be entered together")
        elif not access_token and not client_id:
            errors.append(
                "Enter a Client ID and Client secret from your Dev Dashboard app, "
                "or an Admin API access token from a legacy custom app"
            )

        period = config.get("sales_period", "today")
        if period not in PERIOD_LABELS:
            errors.append("Sales period must be one of: today, week, month")

        threshold = config.get("low_stock_threshold")
        if threshold is None:
            threshold = 0
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            errors.append("Low stock threshold must be a whole number, 0 or more (0 turns it off)")

        errors.extend(self._validate_refresh_seconds(config))
        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Forget the token and order history so a new store or app starts clean."""
        with self._lock:
            self._token = None
            self._token_expires_at = 0.0
            self._reset_order_tracking()

    def _reset_order_tracking(self) -> None:
        self._seen_order_ids: List[str] = []
        self._order_watermark: Optional[datetime] = None
        self._baseline_set = False
        self._pending_alert: Optional[_PendingAlert] = None

    # ------------------------------------------------------------------
    # Shopify API
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Return an Admin API token: the configured one, or a client-credentials token."""
        static = self._setting("access_token", ENV_ACCESS_TOKEN)
        if static:
            return static

        client_id = self._setting("client_id", ENV_CLIENT_ID)
        client_secret = self._setting("client_secret", ENV_CLIENT_SECRET)
        if not client_id or not client_secret:
            raise ShopifyError(
                "Shopify credentials not configured: add a Client ID and Client secret, "
                "or an Admin API access token"
            )

        with self._lock:
            if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token

            shop = self._shop_domain()
            response = requests.post(
                f"https://{shop}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 404:
                raise ShopifyError(f"Store not found: {shop}")
            if response.status_code in (400, 401, 403):
                raise ShopifyError(
                    "Shopify rejected the Client ID and secret. Check them in the Dev Dashboard, "
                    "and make sure the app is installed on this store and both belong to the "
                    "same Shopify organization"
                )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise ShopifyError("Shopify did not return an access token")
            self._token = token
            self._token_expires_at = time.time() + int(payload.get("expires_in") or 86399)
            logger.info("Obtained a new Shopify access token for %s", shop)
            return token

    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Run a GraphQL Admin API query, handling token refresh and throttling."""
        shop = self._shop_domain()
        if not SHOP_DOMAIN_RE.match(shop):
            raise ShopifyError("Store domain not configured: use your-store.myshopify.com")
        url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"

        auth_retried = False
        throttle_retries = 0
        while True:
            token = self._get_access_token()
            response = requests.post(
                url,
                json={"query": query, "variables": variables},
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            status = response.status_code
            if status == 401:
                if not self._uses_static_token() and not auth_retried:
                    # Token revoked or expired early: get a fresh one and retry once.
                    with self._lock:
                        self._token = None
                    auth_retried = True
                    continue
                raise ShopifyError(
                    "Shopify rejected the access token (401). Check your credentials in the plugin settings"
                )
            if status == 402:
                raise ShopifyError("Shopify store is frozen or has an unpaid plan (402)")
            if status == 403:
                raise ShopifyError(
                    "Access denied (403). Make sure the app has the read_orders scope "
                    "(and read_products for low stock)"
                )
            if status == 404:
                raise ShopifyError(f"Store not found: {shop}")
            if status == 423:
                raise ShopifyError("Shopify store is locked (423)")
            if status == 429 and throttle_retries < MAX_THROTTLE_RETRIES:
                throttle_retries += 1
                time.sleep(self._retry_after(response))
                continue
            response.raise_for_status()

            body = response.json()
            errors = body.get("errors")
            if errors:
                codes = self._error_codes(errors)
                if "THROTTLED" in codes and throttle_retries < MAX_THROTTLE_RETRIES:
                    throttle_retries += 1
                    time.sleep(self._throttle_wait(body))
                    continue
                if "ACCESS_DENIED" in codes:
                    raise ShopifyError(
                        "Missing permission: give the app the read_orders scope "
                        "(and read_products for low stock), then release a new version"
                    )
                if "THROTTLED" in codes:
                    raise ShopifyError("Shopify rate limit reached; will retry on the next refresh")
                raise ShopifyError(f"Shopify API error: {self._error_message(errors)}")
            return body.get("data") or {}

    @staticmethod
    def _error_codes(errors: Any) -> set:
        if not isinstance(errors, list):
            return set()
        return {
            (err.get("extensions") or {}).get("code")
            for err in errors
            if isinstance(err, dict)
        }

    @staticmethod
    def _error_message(errors: Any) -> str:
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message", "unknown error"))
        return str(errors)

    @staticmethod
    def _throttle_wait(body: Dict[str, Any]) -> float:
        """Seconds until enough query cost has been restored to retry."""
        cost = (body.get("extensions") or {}).get("cost") or {}
        throttle = cost.get("throttleStatus") or {}
        try:
            needed = float(cost.get("requestedQueryCost", 0)) - float(throttle.get("currentlyAvailable", 0))
            wait = needed / float(throttle.get("restoreRate") or 50)
        except (TypeError, ValueError):
            wait = 1.0
        return min(max(wait, 0.5), MAX_THROTTLE_WAIT_SECONDS)

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        try:
            wait = float(response.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            wait = 1.0
        return min(max(wait, 0.5), MAX_THROTTLE_WAIT_SECONDS)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _counts(self, order: Dict[str, Any]) -> bool:
        """Whether an order counts toward sales and alerts."""
        if order.get("cancelledAt"):
            return False
        if order.get("test") and not self._include_test_orders():
            return False
        return True

    def _period_totals(self, start: datetime) -> Tuple[int, Decimal]:
        """Count and sum the qualifying orders created since *start*."""
        start_utc = start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        search = f"created_at:>='{start_utc}'"
        count = 0
        total = Decimal("0")
        after: Optional[str] = None
        for _ in range(MAX_ORDER_PAGES):
            data = self._graphql(
                PERIOD_ORDERS_QUERY,
                {"first": ORDERS_PAGE_SIZE, "after": after, "query": search},
            )
            connection = data.get("orders") or {}
            for order in connection.get("nodes") or []:
                if not self._counts(order) or parse_datetime(order["createdAt"]) < start:
                    continue
                count += 1
                total += order_amount(order)
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return count, total
            after = page_info.get("endCursor")
        logger.warning("Shopify: stopped after %d pages of orders; totals are partial", MAX_ORDER_PAGES)
        return count, total

    def _low_stock(self, overview: Dict[str, Any], threshold: int) -> Tuple[int, bool, List[Dict[str, Any]]]:
        """Tracked, active variants at or below *threshold*, lowest stock first."""
        connection = overview.get("lowStock") or {}
        variants = [
            v for v in connection.get("nodes") or []
            if (v.get("inventoryItem") or {}).get("tracked")
            and ((v.get("product") or {}).get("status") or "ACTIVE") == "ACTIVE"
            and v.get("inventoryQuantity") is not None
            and v["inventoryQuantity"] <= threshold
        ]
        variants.sort(key=lambda v: v["inventoryQuantity"])
        has_more = bool((connection.get("pageInfo") or {}).get("hasNextPage"))
        return len(variants), has_more, variants

    @staticmethod
    def _variant_label(variant: Dict[str, Any]) -> str:
        product = (variant.get("product") or {}).get("title") or ""
        title = variant.get("title") or ""
        if title and title != "Default Title":
            return board_safe(f"{product} {title}")
        return board_safe(product)

    def fetch_data(self) -> PluginResult:
        try:
            threshold = self._low_stock_threshold()
            overview = self._graphql(
                OVERVIEW_QUERY,
                {
                    "latestFirst": LATEST_ORDERS_COUNT,
                    "includeLowStock": threshold > 0,
                    "lowStockFirst": LOW_STOCK_PAGE_SIZE,
                    "lowStockQuery": f"inventory_quantity:<={threshold} product_status:active",
                },
            )
            shop = overview.get("shop") or {}
            currency = shop.get("currencyCode") or "USD"
            try:
                tz = pytz.timezone(shop.get("ianaTimezone") or "UTC")
            except pytz.exceptions.UnknownTimeZoneError:
                tz = pytz.utc

            now = _utcnow()
            period = self._period()
            order_count, sales = self._period_totals(period_start(now.astimezone(tz), period))

            latest = [
                o for o in (overview.get("latestOrders") or {}).get("nodes") or []
                if self._counts(o)
            ]

            data = self._build_data(
                shop_name=shop.get("name") or "",
                currency=currency,
                period=period,
                order_count=order_count,
                sales=sales,
                latest=latest,
                now=now,
                low_stock=self._low_stock(overview, threshold) if threshold > 0 else None,
            )
            self._record_orders(latest, data)
            return PluginResult(available=True, data=data, formatted_lines=self._format_display(data))
        except ShopifyError as e:
            logger.warning("Shopify: %s", e)
            return PluginResult(available=False, error=str(e))
        except requests.exceptions.Timeout:
            logger.warning("Shopify: request timed out")
            return PluginResult(available=False, error="Timed out contacting Shopify")
        except Exception as e:  # fetch_data must never raise
            logger.exception("Error fetching Shopify data")
            return PluginResult(available=False, error=str(e))

    def _build_data(
        self,
        *,
        shop_name: str,
        currency: str,
        period: str,
        order_count: int,
        sales: Decimal,
        latest: List[Dict[str, Any]],
        now: datetime,
        low_stock: Optional[Tuple[int, bool, List[Dict[str, Any]]]],
    ) -> Dict[str, Any]:
        period_label = PERIOD_LABELS[period]
        sales_display = format_money(sales, currency)
        avg = sales / order_count if order_count else Decimal("0")
        noun = "ORDER" if order_count == 1 else "ORDERS"

        data: Dict[str, Any] = {
            "store_name": board_safe(shop_name),
            "currency": board_safe(currency),
            "period_label": period_label,
            "sales": f"{sales.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
            "sales_display": sales_display,
            "order_count": order_count,
            "avg_order_display": format_money(avg, currency),
            "sales_line": f"{period_label} {sales_display}",
            "orders_line": f"{order_count} {noun}",
        }

        if latest:
            last = latest[0]
            number = board_safe(last.get("name") or "")
            total = format_money(order_amount(last), currency)
            ago = format_ago(parse_datetime(last["createdAt"]), now)
            data.update({
                "last_order_number": number,
                "last_order_total": total,
                "last_order_ago": ago,
                "last_order_line": f"{number} {total} {ago}",
            })
        else:
            data.update({
                "last_order_number": "",
                "last_order_total": "",
                "last_order_ago": "",
                "last_order_line": "NO ORDERS YET",
            })

        if low_stock is None:
            data.update({
                "low_stock_count": 0,
                "low_stock_item": "",
                "low_stock_item_qty": "",
                "low_stock_line": "",
            })
        else:
            count, has_more, variants = low_stock
            count_text = f"{count}+" if has_more else str(count)
            data.update({
                "low_stock_count": count,
                "low_stock_item": self._variant_label(variants[0]) if variants else "",
                "low_stock_item_qty": str(variants[0]["inventoryQuantity"]) if variants else "",
                "low_stock_line": f"{count_text} LOW STOCK" if count else "STOCK OK",
            })
        return data

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    @staticmethod
    def _format_display(data: Dict[str, Any]) -> List[str]:
        """Default 6x22 layout when the user has no template."""
        lines = [
            data["store_name"],
            "",
            data["sales_line"],
            f"{data['orders_line']} AVG {data['avg_order_display']}",
            data["last_order_line"],
            data["low_stock_line"],
        ]
        return [_fit(line) for line in lines]

    def get_formatted_display(self) -> Optional[List[str]]:
        result = self.get_data()
        if not result.available or not result.data:
            return None
        return self._format_display(result.data)

    # ------------------------------------------------------------------
    # New-order alerts (triggers)
    # ------------------------------------------------------------------

    def _record_orders(self, latest: List[Dict[str, Any]], data: Dict[str, Any]) -> None:
        """Remember seen orders and queue an alert for any that are new.

        The first successful fetch only sets a baseline, so a restart never
        re-announces old orders. After that, an order is new if it hasn't been
        seen and isn't older than the newest order already seen (so an old order
        sliding into the latest-orders window, e.g. after a cancellation, isn't
        announced).
        """
        with self._lock:
            ids = [o["id"] for o in latest]
            times = [parse_datetime(o["createdAt"]) for o in latest]
            if not self._baseline_set:
                self._baseline_set = True
                new_orders: List[Dict[str, Any]] = []
            else:
                watermark = self._order_watermark
                new_orders = [
                    o for o, created in zip(latest, times)
                    if o["id"] not in self._seen_order_ids
                    and (watermark is None or created >= watermark)
                ]

            for order_id in ids:
                if order_id not in self._seen_order_ids:
                    self._seen_order_ids.append(order_id)
            del self._seen_order_ids[:-SEEN_ORDER_IDS_LIMIT]
            if times:
                newest = max(times)
                if self._order_watermark is None or newest > self._order_watermark:
                    self._order_watermark = newest

            if new_orders and self.config.get("enable_triggers", True):
                if self._pending_alert is None:
                    self._pending_alert = _PendingAlert()
                # Newest first, matching the API order.
                self._pending_alert.orders = new_orders + self._pending_alert.orders
                self._pending_alert.data = dict(data)

    def check_triggers(self) -> List[TriggerResult]:
        """Return one alert per batch of new orders, exactly once.

        The trigger service calls this every loop tick and resets a trigger's
        timer whenever the same id is returned again, so a pending alert is
        handed over once and then dropped; its duration then runs out normally.
        """
        if not self.config.get("enable_triggers", True):
            return []
        with self._lock:
            alert, self._pending_alert = self._pending_alert, None
        if alert is None or not alert.orders:
            return []

        newest = alert.orders[0]
        try:
            duration = int(self.config.get("alert_duration_seconds") or DEFAULT_ALERT_SECONDS)
        except (TypeError, ValueError):
            duration = DEFAULT_ALERT_SECONDS

        return [TriggerResult(
            triggered=True,
            trigger_id=f"shopify_order_{_numeric_id(newest['id'])}",
            formatted_lines=self._format_alert(alert),
            priority=NEW_ORDER_TRIGGER_PRIORITY,
            duration_seconds=max(duration, 10),
            data=alert.data,
        )]

    def _format_alert(self, alert: _PendingAlert) -> List[str]:
        """Board-sized "new order" page used when no trigger page is picked."""
        rows = self.board.rows if self.board else _DEFAULT_ROWS
        cols = self.board.cols if self.board else _DEFAULT_COLS
        data = alert.data
        count = len(alert.orders)
        text = "NEW ORDER" if count == 1 else f"{count} NEW ORDERS"
        headline = f"{GREEN_TILE} {text} {GREEN_TILE}"
        if count_tiles(headline) > cols:
            headline = text

        newest = alert.orders[0]
        number = board_safe(newest.get("name") or "")
        total = format_money(order_amount(newest), data.get("currency") or "USD")

        if rows >= 6:
            body = ["", headline, "", number, total, data.get("sales_line", "")]
        else:
            body = [headline, number, total]
        body = (body + [""] * rows)[:rows]
        return [_center(line, cols) for line in body]


# Export hook: the loader looks for a module-level `Plugin`.
Plugin = ShopifyPlugin
