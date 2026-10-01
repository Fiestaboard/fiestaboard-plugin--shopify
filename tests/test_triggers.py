"""Tests for new-order alerts (check_triggers)."""

from src.devices import BoardContext
from src.text_to_board import count_tiles

from .conftest import make_order


def add_order(shopify_api, number, created_at, amount):
    order = make_order(number, created_at, amount)
    shopify_api.latest_orders = [order] + shopify_api.latest_orders
    return order


class TestNewOrderAlerts:
    def test_first_fetch_only_sets_a_baseline(self, plugin, shopify_api):
        plugin.fetch_data()

        assert plugin.check_triggers() == []

    def test_new_order_fires_one_alert(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
        plugin.fetch_data()

        triggers = plugin.check_triggers()

        assert len(triggers) == 1
        trigger = triggers[0]
        assert trigger.triggered is True
        assert trigger.trigger_id == "shopify_order_1043"
        assert trigger.priority == 5
        assert trigger.duration_seconds == 60
        assert trigger.data["last_order_number"] == "#1043"
        assert trigger.formatted_lines[1].strip() == "{66} NEW ORDER {66}"
        assert trigger.formatted_lines[3].strip() == "#1043"
        assert trigger.formatted_lines[4].strip() == "$120"

    def test_alert_is_returned_only_once(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
        plugin.fetch_data()
        plugin.check_triggers()

        assert plugin.check_triggers() == []
        plugin.fetch_data()  # same orders again
        assert plugin.check_triggers() == []

    def test_several_new_orders_share_one_alert(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:58:00Z", "20.00")
        add_order(shopify_api, 1044, "2026-09-30T17:59:00Z", "30.00")
        plugin.fetch_data()

        [trigger] = plugin.check_triggers()

        assert trigger.trigger_id == "shopify_order_1044"
        assert trigger.formatted_lines[1].strip() == "{66} 2 NEW ORDERS {66}"

    def test_orders_from_two_fetches_accumulate_until_checked(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:58:00Z", "20.00")
        plugin.fetch_data()
        add_order(shopify_api, 1044, "2026-09-30T17:59:00Z", "30.00")
        plugin.fetch_data()

        [trigger] = plugin.check_triggers()

        assert trigger.trigger_id == "shopify_order_1044"
        assert "2 NEW ORDERS" in trigger.formatted_lines[1]

    def test_older_order_entering_the_window_is_not_announced(self, plugin, shopify_api):
        plugin.fetch_data()
        # An order older than anything seen appears (e.g. a newer one was cancelled).
        shopify_api.latest_orders = shopify_api.latest_orders + [
            make_order(1040, "2026-09-30T14:00:00Z", "15.00")
        ]
        plugin.fetch_data()

        assert plugin.check_triggers() == []

    def test_cancelled_new_order_is_not_announced(self, plugin, shopify_api):
        plugin.fetch_data()
        shopify_api.latest_orders = [
            make_order(1043, "2026-09-30T17:59:00Z", "120.00", cancelled=True)
        ] + shopify_api.latest_orders
        plugin.fetch_data()

        assert plugin.check_triggers() == []

    def test_alerts_can_be_turned_off(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "enable_triggers": False}
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
        plugin.fetch_data()

        assert plugin.check_triggers() == []

    def test_alert_duration_setting(self, plugin, shopify_api, base_config):
        plugin.config = {**base_config, "alert_duration_seconds": 300}
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
        plugin.fetch_data()

        assert plugin.check_triggers()[0].duration_seconds == 300

    def test_config_change_resets_baseline(self, plugin, shopify_api, base_config):
        plugin.fetch_data()
        plugin.config = {**base_config, "shop_domain": "other-store.myshopify.com"}
        plugin.fetch_data()  # new baseline for the new store

        assert plugin.check_triggers() == []
        assert shopify_api.token_requests == 2

    def test_alert_fits_a_note(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:58:00Z", "20.00")
        add_order(shopify_api, 1044, "2026-09-30T17:59:00Z", "30.00")
        plugin.fetch_data()

        with plugin._bound_board(BoardContext("note", rows=3, cols=15)):
            [trigger] = plugin.check_triggers()

        assert len(trigger.formatted_lines) == 3
        assert all(count_tiles(line) <= 15 for line in trigger.formatted_lines)
        # "{66} 2 NEW ORDERS {66}" is 16 tiles, so the tiles are dropped.
        assert trigger.formatted_lines[0].strip() == "2 NEW ORDERS"

    def test_alert_lines_fit_the_flagship(self, plugin, shopify_api):
        plugin.fetch_data()
        add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
        plugin.fetch_data()

        [trigger] = plugin.check_triggers()

        assert len(trigger.formatted_lines) == 6
        assert all(count_tiles(line) <= 22 for line in trigger.formatted_lines)


def test_unreadable_alert_duration_uses_default(plugin, shopify_api, base_config):
    plugin.config = {**base_config, "alert_duration_seconds": "a while"}
    plugin.fetch_data()
    add_order(shopify_api, 1043, "2026-09-30T17:59:00Z", "120.00")
    plugin.fetch_data()

    assert plugin.check_triggers()[0].duration_seconds == 60
