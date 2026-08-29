-- Singular data test guarding the dimension that fct_daily_revenue joins to.
-- More than one active row per customer means the SCD close-out is broken and
-- the revenue mart is silently inflated by the fan-out.
select
    customer_id,
    count(*) as active_rows
from {{ ref('stg_customers') }}
where is_active = true
group by 1
having count(*) > 1
