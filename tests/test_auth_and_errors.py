"""Tests for Shopify authentication, API errors and throttling."""

from unittest.mock import patch

import requests

from .conftest import SHOP_DOMAIN

TOKEN_URL = f"https://{SHOP_DOMAIN}/admin/oauth/access_token"
GRAPHQL_URL = f"https://{SHOP_DOMAIN}/admin/api/2026-07/graphql.json"


def token_headers(shopify_api):
    return [c["headers"]["X-Shopify-Access-Token"] for c in shopify_api.graphql_requests]


class TestClientCredentials:
    def test_exchanges_client_id_and_secret_for_a_token(self, plugin, shopify_api):
        result = plugin.fetch_data()

        assert result.available is True, result.error
        url, kwargs = shopify_api.calls[0]
        assert url == TOKEN_URL
        assert kwargs["data"] == {
            "grant_type": "client_credentials",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        }
        assert set(token_headers(shopify_api)) == {"test_token_1"}
        assert all(url == GRAPHQL_URL for url, _ in shopify_api.calls[1:])

    def test_token_is_reused_between_fetches(self, plugin, shopify_api):
        plugin.fetch_data()
        plugin.fetch_data()

        assert shopify_api.token_requests == 1

    def test_token_is_renewed_shortly_before_it_expires(self, plugin, shopify_api):
        with patch("plugins.shopify.time.time", return_value=1_000_000.0):
            plugin.fetch_data()
        # 86399s lifetime; renewal starts 300s before expiry.
        with patch("plugins.shopify.time.time", return_value=1_000_000.0 + 86399 - 299):
            plugin.fetch_data()

        assert shopify_api.token_requests == 2
        assert token_headers(shopify_api)[-1] == "test_token_2"

    def test_rejected_token_is_replaced_and_request_retried_once(self, plugin, shopify_api):
        shopify_api.queue({"errors": "[API] Invalid API key or access token"}, status_code=401)

        result = plugin.fetch_data()

        assert result.available is True, result.error
        assert shopify_api.token_requests == 2
        assert token_headers(shopify_api)[:2] == ["test_token_1", "test_token_2"]

    def test_repeated_401_gives_up(self, plugin, shopify_api):
        shopify_api.queue(status_code=401)
        shopify_api.queue(status_code=401)

        result = plugin.fetch_data()

        assert result.available is False
        assert "401" in result.error
        assert shopify_api.token_requests == 2

    def test_bad_client_credentials_explain_organization_rule(self, plugin, shopify_api):
        shopify_api.token_status = 400

        result = plugin.fetch_data()

        assert result.available is False
        assert "same Shopify organization" in result.error
        assert shopify_api.graphql_requests == []

    def test_token_endpoint_404_means_unknown_store(self, plugin, shopify_api):
        shopify_api.token_status = 404

        assert plugin.fetch_data().error == f"Store not found: {SHOP_DOMAIN}"

    def test_token_response_without_token(self, plugin, shopify_api):
        with patch("plugins.shopify.requests.post", return_value=_response({"error": "nope"})):
            result = plugin.fetch_data()

        assert result.error == "Shopify did not return an access token"

    def test_error_never_contains_the_secret(self, plugin, shopify_api):
        shopify_api.token_status = 401

        assert "test_client_secret" not in plugin.fetch_data().error


class TestStaticToken:
    def test_legacy_token_is_sent_without_token_exchange(self, plugin, shopify_api):
        plugin.config = {"shop_domain": SHOP_DOMAIN, "access_token": "shpat_example"}

        result = plugin.fetch_data()

        assert result.available is True, result.error
        assert shopify_api.token_requests == 0
        assert set(token_headers(shopify_api)) == {"shpat_example"}

    def test_legacy_token_401_is_not_retried(self, plugin, shopify_api):
        plugin.config = {"shop_domain": SHOP_DOMAIN, "access_token": "shpat_example"}
        shopify_api.queue(status_code=401)

        result = plugin.fetch_data()

        assert result.available is False
        assert len(shopify_api.graphql_requests) == 1

    def test_settings_can_come_from_environment(self, plugin, shopify_api, monkeypatch):
        monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "test-store")
        monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_from_env")
        plugin.config = {"enabled": True}

        result = plugin.fetch_data()

        assert result.available is True, result.error
        assert set(token_headers(shopify_api)) == {"shpat_from_env"}
        assert shopify_api.calls[0][0] == GRAPHQL_URL

    def test_missing_credentials(self, plugin, shopify_api):
        plugin.config = {"shop_domain": SHOP_DOMAIN}

        result = plugin.fetch_data()

        assert result.available is False
        assert "credentials not configured" in result.error
        assert shopify_api.calls == []

    def test_missing_domain(self, plugin, shopify_api):
        plugin.config = {"access_token": "shpat_example"}

        result = plugin.fetch_data()

        assert result.available is False
        assert "Store domain not configured" in result.error
        assert shopify_api.calls == []


class TestApiErrors:
    def test_access_denied_names_the_scope(self, plugin, shopify_api):
        shopify_api.queue({"errors": [{"message": "Access denied for orders field.",
                                       "extensions": {"code": "ACCESS_DENIED"}}]})

        result = plugin.fetch_data()

        assert result.available is False
        assert "read_orders" in result.error

    def test_throttled_request_waits_and_retries(self, plugin, shopify_api, no_sleep):
        shopify_api.queue({
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {"cost": {"requestedQueryCost": 302, "throttleStatus": {
                "maximumAvailable": 1000.0, "currentlyAvailable": 102, "restoreRate": 100.0}}},
        })

        result = plugin.fetch_data()

        assert result.available is True, result.error
        no_sleep.assert_called_once_with(2.0)

    def test_throttle_wait_is_capped(self, plugin, shopify_api, no_sleep):
        shopify_api.queue({
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {"cost": {"requestedQueryCost": 1000, "throttleStatus": {
                "currentlyAvailable": 0, "restoreRate": 50.0}}},
        })

        plugin.fetch_data()

        no_sleep.assert_called_once_with(5.0)

    def test_persistent_throttling_gives_up(self, plugin, shopify_api, no_sleep):
        for _ in range(4):
            shopify_api.queue({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]})

        result = plugin.fetch_data()

        assert result.available is False
        assert "rate limit" in result.error
        assert no_sleep.call_count == 3

    def test_http_429_uses_retry_after(self, plugin, shopify_api, no_sleep):
        shopify_api.queue(status_code=429, headers={"Retry-After": "2.0"})

        result = plugin.fetch_data()

        assert result.available is True, result.error
        no_sleep.assert_called_once_with(2.0)

    def test_other_graphql_error_message_is_shown(self, plugin, shopify_api):
        shopify_api.queue({"errors": [{"message": "Field 'bogus' doesn't exist"}]})

        assert plugin.fetch_data().error == "Shopify API error: Field 'bogus' doesn't exist"

    def test_string_errors_are_shown(self, plugin, shopify_api):
        shopify_api.queue({"errors": "Something broke"})

        assert plugin.fetch_data().error == "Shopify API error: Something broke"

    def test_status_codes_have_clear_messages(self, plugin, shopify_api):
        expected = {
            402: "frozen",
            403: "read_orders",
            404: "Store not found",
            423: "locked",
        }
        for status, fragment in expected.items():
            shopify_api.queue(status_code=status)
            result = plugin.fetch_data()
            assert result.available is False
            assert fragment in result.error, (status, result.error)

    def test_server_error(self, plugin, shopify_api):
        shopify_api.queue(status_code=500)

        result = plugin.fetch_data()

        assert result.available is False
        assert "500" in result.error

    def test_timeout(self, plugin, shopify_api):
        with patch("plugins.shopify.requests.post", side_effect=requests.exceptions.Timeout()):
            result = plugin.fetch_data()

        assert result.error == "Timed out contacting Shopify"

    def test_unexpected_exception_never_raises(self, plugin, shopify_api):
        with patch("plugins.shopify.requests.post", side_effect=RuntimeError("boom")):
            result = plugin.fetch_data()

        assert result.available is False
        assert result.error == "boom"


def _response(body, status_code=200):
    from .conftest import FakeResponse

    return FakeResponse(body, status_code)


class TestRetryWaits:
    def test_unreadable_retry_after_waits_one_second(self, plugin, shopify_api, no_sleep):
        shopify_api.queue(status_code=429, headers={"Retry-After": "soon"})

        plugin.fetch_data()

        no_sleep.assert_called_once_with(1.0)

    def test_unreadable_throttle_status_waits_one_second(self, plugin, shopify_api, no_sleep):
        shopify_api.queue({
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {"cost": {"requestedQueryCost": "n/a", "throttleStatus": {}}},
        })

        plugin.fetch_data()

        no_sleep.assert_called_once_with(1.0)
