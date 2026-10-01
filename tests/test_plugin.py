"""Tests for the Shopify plugin: data, formatting, config and manifest."""

from datetime import datetime
from decimal import Decimal

import pytest
import pytz

from plugins.shopify import (
    ShopifyPlugin,
    board_safe,
    format_ago,
    format_money,
    normalize_shop_domain,
    period_start,
)
from src.board_chars import BoardChars
from src.text_to_board import count_tiles

from .conftest import make_order, make_variant


def period_search(shopify_api):
    """The `query` search string sent with the first period-orders request."""
    for call in shopify_api.graphql_requests:
        if "FiestaBoardPeriodOrders" in call["json"]["query"]:
            return call["json"]["variables"]["query"]
    raise AssertionError("no period orders request was sent")


class TestPluginBasics:
    def test_plugin_id_matches_manifest(self, plugin, manifest_data):
        assert plugin.plugin_id == "shopify" == manifest_data["id"]

    def test_module_exports_plugin(self):
        import plugins.shopify as module

        assert module.Plugin is ShopifyPlugin


class TestSalesData:
    def test_totals_sum_orders_in_period(self, plugin, shopify_api):
        result = plugin.fetch_data()

        assert result.available is True, result.error
        assert result.data["sales"] == "126.50"
        assert result.data["sales_display"] == "$127"
        assert result.data["order_count"] == 2
        assert result.data["orders_line"] == "2 ORDERS"
        assert result.data["avg_order_display"] == "$63"
        assert result.data["sales_line"] == "TODAY $127"

    def test_single_order_uses_singular_noun(self, plugin, shopify_api):
        shopify_api.period_pages = [[make_order(1042, "2026-09-30T17:48:00Z", "86.00")]]

        assert plugin.fetch_data().data["orders_line"] == "1 ORDER"

    def test_cancelled_and_test_orders_are_excluded(self, plugin, shopify_api):
        shopify_api.period_pages = [[
            make_order(1044, "2026-09-30T17:50:00Z", "10.00", test=True),
            make_order(1043, "2026-09-30T17:49:00Z", "20.00", cancelled=True),
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
        ]]

        data = plugin.fetch_data().data

        assert data["order_count"] == 1
        assert data["sales"] == "86.00"

    def test_include_test_orders_counts_them(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "include_test_orders": True}
        shopify_api.period_pages = [[
            make_order(1044, "2026-09-30T17:50:00Z", "10.00", test=True),
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
        ]]

        data = plugin.fetch_data().data

        assert data["order_count"] == 2
        assert data["sales"] == "96.00"

    def test_pages_are_followed_and_summed(self, plugin, shopify_api):
        shopify_api.period_pages = [
            [make_order(1003, "2026-09-30T17:00:00Z", "10.00")],
            [make_order(1002, "2026-09-30T16:00:00Z", "20.00")],
            [make_order(1001, "2026-09-30T15:00:00Z", "30.00")],
        ]

        data = plugin.fetch_data().data

        assert data["order_count"] == 3
        assert data["sales"] == "60.00"
        afters = [
            c["json"]["variables"]["after"]
            for c in shopify_api.graphql_requests
            if "FiestaBoardPeriodOrders" in c["json"]["query"]
        ]
        assert afters == [None, "1", "2"]

    def test_today_starts_at_store_midnight(self, plugin, shopify_api):
        plugin.fetch_data()

        # Midnight 2026-09-30 in New York (EDT) is 04:00 UTC.
        assert period_search(shopify_api) == "created_at:>='2026-09-30T04:00:00Z'"

    def test_orders_before_period_start_are_ignored(self, plugin, shopify_api):
        shopify_api.period_pages = [[
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
            make_order(1030, "2026-09-30T03:59:00Z", "500.00"),  # 23:59 the day before, local
        ]]

        assert plugin.fetch_data().data["order_count"] == 1

    def test_week_period(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "sales_period": "week"}

        data = plugin.fetch_data().data

        # Monday 2026-09-28 00:00 EDT.
        assert period_search(shopify_api) == "created_at:>='2026-09-28T04:00:00Z'"
        assert data["period_label"] == "THIS WEEK"
        assert data["sales_line"] == "THIS WEEK $127"

    def test_month_period(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "sales_period": "month"}

        data = plugin.fetch_data().data

        assert period_search(shopify_api) == "created_at:>='2026-09-01T04:00:00Z'"
        assert data["period_label"] == "THIS MONTH"

    def test_unknown_period_falls_back_to_today(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "sales_period": "fortnight"}

        assert plugin.fetch_data().data["period_label"] == "TODAY"

    def test_unknown_store_timezone_falls_back_to_utc(self, plugin, shopify_api):
        shopify_api.shop = {**shopify_api.shop, "ianaTimezone": "Mars/Olympus_Mons"}

        plugin.fetch_data()

        assert period_search(shopify_api) == "created_at:>='2026-09-30T00:00:00Z'"

    def test_non_dollar_currency_uses_code(self, plugin, shopify_api):
        shopify_api.shop = {**shopify_api.shop, "currencyCode": "EUR"}
        shopify_api.period_pages = [[make_order(1042, "2026-09-30T17:48:00Z", "1284.40")]]

        data = plugin.fetch_data().data

        assert data["sales_display"] == "EUR 1,284"
        assert data["currency"] == "EUR"

    def test_store_name_is_board_safe(self, plugin, shopify_api):
        shopify_api.shop = {**shopify_api.shop, "name": "Café {Délice} 🌮"}

        assert plugin.fetch_data().data["store_name"] == "CAFE DELICE"


class TestLatestOrder:
    def test_latest_order_fields(self, plugin, shopify_api):
        data = plugin.fetch_data().data

        assert data["last_order_number"] == "#1042"
        assert data["last_order_total"] == "$86"
        assert data["last_order_ago"] == "12M AGO"
        assert data["last_order_line"] == "#1042 $86 12M AGO"

    def test_latest_skips_cancelled_orders(self, plugin, shopify_api):
        shopify_api.latest_orders = [
            make_order(1043, "2026-09-30T17:55:00Z", "99.00", cancelled=True),
            make_order(1042, "2026-09-30T17:48:00Z", "86.00"),
        ]

        assert plugin.fetch_data().data["last_order_number"] == "#1042"

    def test_no_orders(self, plugin, shopify_api):
        shopify_api.latest_orders = []
        shopify_api.period_pages = [[]]

        data = plugin.fetch_data().data

        assert data["last_order_line"] == "NO ORDERS YET"
        assert data["last_order_number"] == ""
        assert data["order_count"] == 0
        assert data["sales_display"] == "$0"
        assert data["avg_order_display"] == "$0"


class TestLowStock:
    def test_disabled_by_default(self, plugin, shopify_api):
        data = plugin.fetch_data().data

        overview = shopify_api.graphql_requests[0]["json"]["variables"]
        assert overview["includeLowStock"] is False
        assert data["low_stock_line"] == ""
        assert data["low_stock_count"] == 0

    def test_counts_tracked_active_variants_at_or_below_threshold(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": 5}
        shopify_api.low_stock = [
            make_variant("Ceramic Mug", "Blue", 4),
            make_variant("Tote Bag", "Default Title", 1),
            make_variant("Gift Card", "Default Title", 0, tracked=False),
            make_variant("Old Hat", "Default Title", 0, status="DRAFT"),
            make_variant("Sticker", "Default Title", 9),
        ]

        data = plugin.fetch_data().data

        overview = shopify_api.graphql_requests[0]["json"]["variables"]
        assert overview["includeLowStock"] is True
        assert overview["lowStockQuery"] == "inventory_quantity:<=5 product_status:active"
        assert data["low_stock_count"] == 2
        assert data["low_stock_line"] == "2 LOW STOCK"
        assert data["low_stock_item"] == "TOTE BAG"
        assert data["low_stock_item_qty"] == "1"

    def test_variant_title_is_appended(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": 5}
        shopify_api.low_stock = [make_variant("Ceramic Mug", "Blue", 2)]

        assert plugin.fetch_data().data["low_stock_item"] == "CERAMIC MUG BLUE"

    def test_more_results_marked_with_plus(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": 5}
        shopify_api.low_stock = [make_variant("Ceramic Mug", "Blue", 2)]
        shopify_api.low_stock_has_more = True

        assert plugin.fetch_data().data["low_stock_line"] == "1+ LOW STOCK"

    def test_nothing_low(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": 5}

        data = plugin.fetch_data().data

        assert data["low_stock_line"] == "STOCK OK"
        assert data["low_stock_item"] == ""


class TestDisplay:
    def test_formatted_display_is_six_board_lines(self, plugin, shopify_api):
        lines = plugin.get_formatted_display()

        assert isinstance(lines, list)
        assert len(lines) == 6
        assert all(count_tiles(line) <= 22 for line in lines)
        assert lines[0] == "MY TEST STORE"
        assert lines[2] == "TODAY $127"
        assert lines[3] == "2 ORDERS AVG $63"

    def test_formatted_display_none_when_unavailable(self, plugin, shopify_api):
        shopify_api.token_status = 401

        assert plugin.get_formatted_display() is None

    def test_result_includes_formatted_lines(self, plugin, shopify_api):
        result = plugin.fetch_data()

        assert result.formatted_lines == plugin._format_display(result.data)

    def test_every_string_value_is_board_safe(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": 5}
        shopify_api.shop = {**shopify_api.shop, "name": "Ünïcode ✨ Shop"}
        shopify_api.low_stock = [make_variant("Tasse à café", "Größe L", 1)]

        data = plugin.fetch_data().data

        for key, value in data.items():
            for ch in str(value):
                assert ch == " " or BoardChars.get_char_code(ch) is not None, f"{key}={value!r}"
                assert ch not in "{}", f"{key}={value!r}"


class TestManifestContract:
    def test_returns_exactly_the_declared_variables(self, plugin, shopify_api, manifest_data):
        data = plugin.fetch_data().data

        assert set(data) == set(manifest_data["variables"]["simple"])

    def test_all_variables_have_descriptions_and_valid_groups(self, manifest_data):
        groups = set(manifest_data["variables"]["groups"])
        for name, meta in manifest_data["variables"]["simple"].items():
            assert meta.get("description"), name
            assert meta.get("group") in groups, name

    def test_groups_have_labels(self, manifest_data):
        for group_id, group in manifest_data["variables"]["groups"].items():
            assert group.get("label"), group_id

    def test_examples_fit_max_length(self, manifest_data):
        for name, meta in manifest_data["variables"]["simple"].items():
            if "max_length" in meta:
                assert len(meta["example"]) <= meta["max_length"], name

    def test_secrets_use_password_widget(self, manifest_data):
        props = manifest_data["settings_schema"]["properties"]
        assert props["client_secret"]["ui:widget"] == "password"
        assert props["access_token"]["ui:widget"] == "password"

    def test_declares_triggers(self, manifest_data):
        assert manifest_data["supports_triggers"] is True


class TestValidateConfig:
    def test_valid_client_credentials(self, plugin, base_config):
        assert plugin.validate_config(base_config) == []

    def test_valid_legacy_token(self, plugin):
        assert plugin.validate_config({"shop_domain": "test-store", "access_token": "shpat_example"}) == []

    def test_missing_domain(self, plugin, base_config):
        errors = plugin.validate_config({**base_config, "shop_domain": ""})
        assert errors == ["Store domain is required, e.g. your-store.myshopify.com"]

    def test_custom_domain_rejected(self, plugin, base_config):
        errors = plugin.validate_config({**base_config, "shop_domain": "shop.example.com"})
        assert len(errors) == 1 and "myshopify.com" in errors[0]

    def test_no_credentials(self, plugin):
        errors = plugin.validate_config({"shop_domain": "test-store.myshopify.com"})
        assert len(errors) == 1 and "Client ID and Client secret" in errors[0]

    def test_client_id_without_secret(self, plugin):
        errors = plugin.validate_config({"shop_domain": "test-store.myshopify.com", "client_id": "test_id"})
        assert errors == ["Client ID and Client secret must be entered together"]

    def test_credentials_from_env(self, plugin, monkeypatch):
        monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "test-store.myshopify.com")
        monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_example")
        assert plugin.validate_config({}) == []

    def test_bad_period(self, plugin, base_config):
        assert plugin.validate_config({**base_config, "sales_period": "year"}) == [
            "Sales period must be one of: today, week, month"
        ]

    @pytest.mark.parametrize("threshold", [-1, "5", True, 2.5])
    def test_bad_threshold(self, plugin, base_config, threshold):
        errors = plugin.validate_config({**base_config, "low_stock_threshold": threshold})
        assert len(errors) == 1 and "Low stock threshold" in errors[0]

    def test_refresh_bounds_checked(self, plugin, base_config):
        assert plugin.validate_config({**base_config, "refresh_seconds": 5})


class TestHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("test-store", "test-store.myshopify.com"),
        ("Test-Store.myshopify.com", "test-store.myshopify.com"),
        ("https://test-store.myshopify.com/admin/orders", "test-store.myshopify.com"),
        ("  ", ""),
    ])
    def test_normalize_shop_domain(self, raw, expected):
        assert normalize_shop_domain(raw) == expected

    @pytest.mark.parametrize("amount,currency,expected", [
        ("1284.49", "USD", "$1,284"),
        ("1284.50", "CAD", "$1,285"),
        ("0", "USD", "$0"),
        ("-12.00", "USD", "-$12"),
        ("99.99", "GBP", "GBP 100"),
    ])
    def test_format_money(self, amount, currency, expected):
        assert format_money(Decimal(amount), currency) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (30, "JUST NOW"), (60 * 12, "12M AGO"), (3600 * 3, "3H AGO"), (86400 * 2, "2D AGO"),
    ])
    def test_format_ago(self, seconds, expected):
        now = datetime(2026, 9, 30, 18, 0, tzinfo=pytz.utc)
        then = datetime.fromtimestamp(now.timestamp() - seconds, tz=pytz.utc)
        assert format_ago(then, now) == expected

    def test_board_safe_keeps_supported_punctuation(self):
        assert board_safe("Order #1042: $86 (paid)!") == "ORDER #1042: $86 (PAID)!"

    def test_period_start_uses_standard_time_after_dst_ends(self):
        tz = pytz.timezone("America/New_York")
        now_local = tz.localize(datetime(2026, 11, 2, 9, 0))  # Monday after DST ends
        start = period_start(now_local, "today")
        assert start.astimezone(pytz.utc) == datetime(2026, 11, 2, 5, 0, tzinfo=pytz.utc)

    def test_period_start_month_before_dst_change(self):
        tz = pytz.timezone("America/New_York")
        now_local = tz.localize(datetime(2026, 11, 20, 9, 0))
        start = period_start(now_local, "month")
        # Nov 1 midnight is still EDT (DST ends at 2am).
        assert start.astimezone(pytz.utc) == datetime(2026, 11, 1, 4, 0, tzinfo=pytz.utc)


class TestDefensiveConfig:
    def test_non_numeric_threshold_turns_low_stock_off(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "low_stock_threshold": "lots"}

        data = plugin.fetch_data().data

        assert shopify_api.graphql_requests[0]["json"]["variables"]["includeLowStock"] is False
        assert data["low_stock_line"] == ""

    def test_order_pages_are_capped(self, plugin, shopify_api):
        from unittest.mock import patch

        shopify_api.period_pages = [
            [make_order(1003, "2026-09-30T17:00:00Z", "10.00")],
            [make_order(1002, "2026-09-30T16:00:00Z", "20.00")],
            [make_order(1001, "2026-09-30T15:00:00Z", "30.00")],
        ]
        with patch("plugins.shopify.MAX_ORDER_PAGES", 2):
            data = plugin.fetch_data().data

        assert data["order_count"] == 2
        assert data["sales"] == "30.00"
