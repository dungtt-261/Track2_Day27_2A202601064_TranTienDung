-- Freshness as a data test (the batch arrives as a seed, so dbt's native
-- `source freshness` is not available here). Warns rather than blocks: a stale
-- batch is an availability problem, not a correctness one.
{{ config(severity='warn') }}
select
    max(updated_at) as latest_update,
    {{ dbt.current_timestamp() }} as checked_at
from {{ ref('stg_orders') }}
having date_diff('hour', max(updated_at), {{ dbt.current_timestamp() }}::timestamp) > 24
