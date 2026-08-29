{% test dbt_utils_free_positive_rows(model, column_name) %}
-- Small home-made generic test so the lab stays package-free:
-- fails for any row where the column is not strictly positive.
select *
from {{ model }}
where {{ column_name }} is null or {{ column_name }} <= 0
{% endtest %}
