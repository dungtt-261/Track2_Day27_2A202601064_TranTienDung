-- Singular BUSINESS test: the mart must reproduce exactly the revenue that
-- exists in staging. Any join fan-out, dropped partition or double-count shows
-- up here even if every column-level test passes.
with expected as (
    select
        order_date,
        count(*) as expected_rows,
        sum(amount_usd) as expected_revenue
    from {{ ref('stg_orders') }}
    where status = 'completed'
    group by 1
)
select
    e.order_date,
    e.expected_rows,
    f.completed_order_rows,
    e.expected_revenue,
    f.daily_revenue
from expected e
join {{ ref('fct_daily_revenue') }} f using (order_date)
where f.completed_order_rows <> e.expected_rows
   or abs(f.daily_revenue - e.expected_revenue) > 0.01
