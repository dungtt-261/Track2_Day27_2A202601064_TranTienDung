-- Daily completed-order revenue for the CEO dashboard.
--
-- FAN-OUT HAZARD (fixed): joining a fact table to a dimension that is not unique
-- on its join key silently multiplies the fact rows. When the SCD close-out job
-- fails to set `valid_to` on the previous version, a customer ends up with two
-- `is_active = true` rows and every one of their orders is counted twice. There
-- is no SQL error, no null, no duplicate order_id - only revenue that is wrong.
-- `unit_tests.yml::revenue_not_inflated_by_duplicate_active_customer` reproduces
-- it, and `tests/assert_one_active_row_per_customer.sql` guards the dimension.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),

-- Exactly one active row per customer: newest version wins.
active_customers as (
    select *
    from {{ ref('stg_customers') }}
    where is_active = true
    qualify row_number() over (
        partition by customer_id
        order by valid_from desc nulls last
    ) = 1
)

select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join active_customers c
    on o.customer_id = c.customer_id
group by 1
order by 1
