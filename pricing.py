"""Selling price calculation.

price = (cost + shipping) x (1 + platform_fee_percent/100 + min_profit_percent/100)

Confirmed with the seller: platform fee and minimum profit are both simple
additions on top of cost, not grossed up - 20% fee + 20% minimum profit means
a flat 40% total markup over cost, applied uniformly to every product (no
separate higher "default markup" tier). If a product's category carries a
higher OnBuy commission than 20%, pass that category's platform_fee_percent
in - the margin portion still never drops below MIN_PROFIT_PERCENT (20%)
regardless of what the fee is.
"""

MIN_PROFIT_PERCENT = 20
PLATFORM_FEE_PERCENT = 20  # override per call if a category's OnBuy commission differs


def calculate_selling_price(
    cost_price,
    shipping_cost=0.0,
    *,
    min_profit_percent=MIN_PROFIT_PERCENT,
    platform_fee_percent=PLATFORM_FEE_PERCENT,
):
    if cost_price <= 0:
        return 0.0

    total_cost = cost_price + shipping_cost
    effective_margin_percent = max(min_profit_percent, MIN_PROFIT_PERCENT)  # margin floor, even if a lower value is ever passed in
    total_markup_percent = platform_fee_percent + effective_margin_percent

    return round(total_cost * (1 + total_markup_percent / 100), 2)
