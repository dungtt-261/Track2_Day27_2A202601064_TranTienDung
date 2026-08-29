PYTHON ?= python

.PHONY: reset baseline tests tests-all gx dbt dashboard generate incident clean-faults \
        marquez-up marquez-down lineage lineage-dbt

reset:
	$(PYTHON) scripts/reset_lab.py

baseline:
	$(PYTHON) scripts/run_baseline.py

tests:
	pytest tests_public -q

tests-all:
	pytest tests_public tests -q

gx:
	$(PYTHON) gx/validate_orders.py

dbt:
	$(PYTHON) scripts/sync_dbt_seeds.py
	dbt build --project-dir dbt_project --profiles-dir dbt_project

dashboard:
	streamlit run dashboard/app.py

generate:
	$(PYTHON) scripts/generate_data.py --rows 600 --days 42 --seed 27

# Full evidence bundle for one incident: contracts + GX + dbt + observability.
incident:
	-$(PYTHON) gx/validate_orders.py
	-$(MAKE) dbt
	$(PYTHON) scripts/run_baseline.py

# --- OpenLineage / Marquez -------------------------------------------------
# API http://localhost:5000 · UI http://localhost:3000
marquez-up:
	docker compose -f marquez/docker-compose.yml up -d
	@echo "waiting for Marquez API..."
	@until curl -sf http://localhost:5000/api/v1/namespaces >/dev/null; do sleep 2; done
	@echo "Marquez ready: API http://localhost:5000 · UI http://localhost:3000"

marquez-down:
	docker compose -f marquez/docker-compose.yml down -v

# Publish the lab lineage + latest contract results to Marquez, then read it back.
lineage:
	$(PYTHON) scripts/emit_lineage.py

# Same, but the graph comes from the real dbt target/manifest.json.
lineage-dbt:
	$(PYTHON) scripts/emit_lineage.py --source dbt --namespace dbt-lab

clean-faults:
	rm -rf data/quarantine reports/run_history.jsonl
	$(PYTHON) scripts/reset_lab.py
