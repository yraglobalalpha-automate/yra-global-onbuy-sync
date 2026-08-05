"""Selling price calculation.

price = (cost + shipping) x (1 + total_markup_percent/100)

Tiered total markup by product cost (user policy 2026-07-21, rewritten
2026-08-05, all stores), where "total" includes the OnBuy platform fee,
mirroring how the original flat 40% (20% fee + 20% profit) was defined:

  cost + shipping  under GBP 5    -> 100% total markup (2026-07-21)
  cost + shipping  GBP 5 to 10    -> 100% total markup (was 80%; +20%
                                      2026-08-05)
  cost + shipping  GBP 10 to 30   ->  60% total markup (40% profit + 20%
                                      fee; 2026-08-05)
  cost + shipping  GBP 30 to 100  ->  60% total markup (was 40%; +20%
                                      2026-08-05)
  cost + shipping  over GBP 100   ->  50% total markup (was 40%; +10%
                                      2026-08-05)

Cheap products carried too little absolute profit at a flat 40% - a GBP 3
item earned pennies after the fee. The bands apply to the same base the
markup multiplies (cost + shipping). Band edges: the first bound is strict
("under 5"), every later band's upper bound is inclusive - exactly GBP 10
falls in the 100% band, exactly GBP 30 and exactly GBP 100 in the 60%
band; strictly above 100 gets 50%. This applies to already-listed products
too: every sweep recalculates and raises any price below the formula
(max(existing, formula) in generate_xml.py) - only a manually-set price
ABOVE the formula is left alone, per the never-lower rule below.

The old signature (min_profit_percent/platform_fee_percent overrides) is
kept for compatibility: when a caller passes a HIGHER fee than 20% for a
pricier category, the extra fee is added on top of the band so the profit
portion never shrinks below what the band intends.
"""

MIN_PROFIT_PERCENT = 20
PLATFORM_FEE_PERCENT = 20  # override per call if a category's OnBuy commission differs

# (upper cost bound inclusive, total markup %) - checked in order; None = no
# bound. Adjacent bands may share a rate - they are kept separate so each
# line traces to the policy decision that set it.
MARGIN_BANDS = (
    (5.0, 100),    # under GBP 5 (2026-07-21)
    (10.0, 100),   # GBP 5-10 inclusive (80% -> 100%, 2026-08-05)
    (30.0, 60),    # over GBP 10 up to 30 inclusive (2026-08-05)
    (100.0, 60),   # over GBP 30 up to 100 inclusive (40% -> 60%, 2026-08-05)
    (None, 50),    # above GBP 100 (40% -> 50%, 2026-08-05)
)


def total_markup_percent(total_cost):
    # First band is strict-below (exactly 5 -> 80% band); every later
    # band's upper bound is inclusive - same edge rules as documented above.
    if total_cost < MARGIN_BANDS[0][0]:
        return MARGIN_BANDS[0][1]
    for bound, markup in MARGIN_BANDS[1:]:
        if bound is None or total_cost <= bound:
            return markup


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
