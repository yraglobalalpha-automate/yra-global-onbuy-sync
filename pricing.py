"""Selling price calculation.

price = (cost + shipping) x (1 + total_markup_percent/100)

Tiered total markup by product cost (user policy 2026-07-21, all stores),
where "total" includes the OnBuy platform fee, mirroring how the original
flat 40% (20% fee + 20% profit) was defined:

  cost + shipping  under GBP 5   -> 100% total markup
  cost + shipping  GBP 5 to 10   ->  80% total markup
  cost + shipping  over GBP 10   ->  40% total markup (the original rate)

Cheap products carried too little absolute profit at a flat 40% - a GBP 3
item earned pennies after the fee. The bands apply to the same base the
markup multiplies (cost + shipping). Band edges: exactly GBP 5 falls in
the 80% band ("5-10"), exactly GBP 10 falls in the 80% band too; strictly
above 10 gets 40%.

The old signature (min_profit_percent/platform_fee_percent overrides) is
kept for compatibility: when a caller passes a HIGHER fee than 20% for a
pricier category, the extra fee is added on top of the band so the profit
portion never shrinks below what the band intends.
"""

MIN_PROFIT_PERCENT = 20
PLATFORM_FEE_PERCENT = 20  # override per call if a category's OnBuy commission differs

# (upper cost bound inclusive, total markup %) - checked in order; None = no bound
MARGIN_BANDS = (
    (5.0, 100),   # under GBP 5 (exactly 5 goes to the next band)
    (10.0, 80),   # GBP 5-10 inclusive
    (None, 40),   # everything above
)


def total_markup_percent(total_cost):
    if total_cost < MARGIN_BANDS[0][0]:
        return MARGIN_BANDS[0][1]
    if total_cost <= MARGIN_BANDS[1][0]:
        return MARGIN_BANDS[1][1]
    return MARGIN_BANDS[2][1]


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
    markup = total_markup_percent(total_cost)
    # A category fee above the standard 20% stacks on top, so the band's
    # intended profit portion survives fee-heavy categories.
    extra_fee = max(0, platform_fee_percent - PLATFORM_FEE_PERCENT)
    return round(total_cost * (1 + (markup + extra_fee) / 100), 2)
