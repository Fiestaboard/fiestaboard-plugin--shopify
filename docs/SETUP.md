# Shopify Setup Guide

Connect FiestaBoard to your Shopify store to show sales, orders and low stock, and to get an alert page for every new order.

## Overview

**What it does:**

- Shows sales, order count and average order for today, this week or this month
- Shows your latest order and how long ago it came in
- Counts products that are running low (optional)
- Interrupts the board with a **NEW ORDER** page when an order comes in (optional)

**Prerequisites:**

- A Shopify store where you are the owner, or a staff member allowed to develop apps
- About 10 minutes to create a small, read-only app for your store in the Shopify **Dev Dashboard**

FiestaBoard only reads from your store. It never changes orders, products or settings.

> **Which sign-in method?** Since January 1, 2026, Shopify no longer lets you create custom apps inside the Shopify admin. New apps are made in the Dev Dashboard and give you a **Client ID** and **Client secret**, which is what this guide uses. If you already have a custom app from before 2026 with an **Admin API access token** (it starts with `shpat_`), you can skip to [Using a legacy access token](#using-a-legacy-access-token).

## Quick Setup

### 1. Create an app in the Dev Dashboard

1. Go to the [Shopify Dev Dashboard](https://dev.shopify.com/dashboard/) and sign in with the account that owns your store.
2. Open **Apps** and create a new app. Name it something like `FiestaBoard`.
3. In the app's version configuration, select these **Admin API access scopes**:
   - `read_orders` (required)
   - `read_products` (only if you want low-stock tracking)
4. **Release** the version so the scopes take effect.
5. **Install** the app on your store from the Dev Dashboard.
6. Open the app's **Settings** and copy the **Client ID** and **Client secret**.

Treat the Client secret like a password: it gives read access to your store's orders.

The client credentials method only works when the app and the store belong to the **same Shopify organization**. Create the app while signed in to the organization that owns the store.

### 2. Enable the Plugin

In the FiestaBoard web UI:

1. Go to **Integrations**
2. Find **Shopify** and toggle it **On**

### 3. Configure Shopify

1. Click **Configure**
2. **Store Domain**: your `.myshopify.com` address, for example `your-store.myshopify.com`. In the Shopify admin, it's under **Settings > Domains**. Your custom domain won't work here.
3. Paste the **Client ID** and **Client Secret**
4. Optional:
   - **Sales Period**: today, this week (starting Monday) or this month
   - **Low Stock Threshold**: for example `5` to flag variants with 5 or fewer units
   - **New Order Alerts** and **Alert Duration**
   - **Include Test Orders**: turn this on for a development store, where every order is a test order
5. Click **Save Changes**

### 4. Create a Board Template

Create a page from the plugin's demo, or add variables to your own page. Center-align the lines:

```jinja
{{shopify.store_name}}

{66} {{shopify.sales_line}}
{{shopify.orders_line}} AVG {{shopify.avg_order_display}}
{{shopify.last_order_line}}
{{shopify.low_stock_line}}
```

To design your own new-order page, create a page that uses `{{shopify.last_order_number}}` and `{{shopify.last_order_total}}`. Then choose it as the **Alert Page** in the plugin settings.

### 5. View on Your Board

The board updates on each refresh (every 2 minutes by default). New-order alerts appear within one refresh of the order being placed. The first check after FiestaBoard starts only records existing orders, so older orders are never announced.

## Template Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{{shopify.sales_line}}` | Period and sales together | `TODAY $1,284` |
| `{{shopify.sales_display}}` | Sales in whole currency units | `$1,284` |
| `{{shopify.sales}}` | Sales with cents | `1284.50` |
| `{{shopify.period_label}}` | The period the totals cover | `TODAY` |
| `{{shopify.orders_line}}` | Order count with the word ORDERS | `23 ORDERS` |
| `{{shopify.order_count}}` | Number of orders | `23` |
| `{{shopify.avg_order_display}}` | Average order value | `$56` |
| `{{shopify.last_order_line}}` | Latest order summary | `#1042 $86 12M AGO` |
| `{{shopify.last_order_number}}` | Latest order number | `#1042` |
| `{{shopify.last_order_total}}` | Latest order total | `$86` |
| `{{shopify.last_order_ago}}` | Age of the latest order | `12M AGO` |
| `{{shopify.low_stock_line}}` | Low-stock summary | `4 LOW STOCK` |
| `{{shopify.low_stock_count}}` | Variants at or below the threshold | `4` |
| `{{shopify.low_stock_item}}` | Variant with the least stock | `CERAMIC MUG BLUE` |
| `{{shopify.low_stock_item_qty}}` | Units left of that variant | `2` |
| `{{shopify.store_name}}` | Store name | `MY TEST STORE` |
| `{{shopify.currency}}` | Currency code | `USD` |

## Configuration Reference

| Setting | Required | Default | Description |
|---------|----------|---------|-------------|
| Store Domain | Yes | | `your-store.myshopify.com` (the bare handle `your-store` also works) |
| Client ID | With Client Secret | | From the Dev Dashboard app's Settings |
| Client Secret | With Client ID | | From the Dev Dashboard app's Settings |
| Admin API Access Token | Instead of Client ID/Secret | | Legacy custom apps only (`shpat_...`) |
| Sales Period | No | Today | Today, This week (starts Monday) or This month, in the store's timezone |
| Include Test Orders | No | Off | Count orders placed with Shopify's test gateway |
| Low Stock Threshold | No | 0 (off) | Flag active, tracked variants with this many units or fewer |
| New Order Alerts | No | On | Show an alert page for new orders |
| Alert Duration (seconds) | No | 60 | How long the alert stays up (10–3600) |
| Alert Page | No | Built-in | Page to show for new orders |
| Refresh Interval (seconds) | No | 120 | How often to check Shopify (60–3600) |

**Environment variables** (alternatives to the UI settings):

| Variable | Description |
|----------|-------------|
| `SHOPIFY_SHOP_DOMAIN` | Store domain |
| `SHOPIFY_CLIENT_ID` | Dev Dashboard app Client ID |
| `SHOPIFY_CLIENT_SECRET` | Dev Dashboard app Client secret |
| `SHOPIFY_ACCESS_TOKEN` | Legacy Admin API access token |

### Using a legacy access token

If you created a custom app in the Shopify admin before 2026, you can keep using it. Paste its **Admin API access token** (it starts with `shpat_`) into **Admin API Access Token** and leave Client ID and Client Secret empty. The app needs the same `read_orders` scope, plus `read_products` for low stock.

## Troubleshooting

**"Shopify rejected the Client ID and secret…"**

- Copy both values again from the app's **Settings** page in the Dev Dashboard. Watch for extra spaces.
- Make sure the app is **installed** on the store.
- The app and the store must belong to the same Shopify organization. This method can't reach a store owned by someone else, such as a client's store.

**"Missing permission: give the app the read_orders scope…"** or **"Access denied (403)"**

- Add `read_orders` (and `read_products` for low stock) to the app's scopes, **release a new version**, and approve the updated scopes on the store if asked.

**"Store not found"**

- Use the `.myshopify.com` address from **Settings > Domains**, not your custom domain.

**Sales show $0 on a development store**

- Orders on development stores are test orders, which are skipped by default. Turn on **Include Test Orders**.

**Totals look different from Shopify Analytics**

- The plugin adds up each order's current total after refunds and edits, including tax and shipping. It skips cancelled orders. Shopify's "Total sales" report subtracts returns and discounts differently, so the numbers can differ slightly.
- The day starts at midnight in your **store's** timezone, set in Shopify under **Settings > General**.

**"Shopify rate limit reached"**

- The plugin already waits and retries. If you see this often, raise the refresh interval. For very busy stores, prefer the Today period over This month.

**No new-order alerts**

- Check that **New Order Alerts** is on. The first check after a restart only records existing orders.
- Cancelled orders don't trigger alerts, and neither do test orders unless **Include Test Orders** is on.

**Plugin shows "Not Available"**

- The error next to the plugin on the Integrations page says what went wrong. For more detail, check the logs: `docker compose logs -f`
