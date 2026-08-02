CLUSTER := reminder-demo
IMAGE := reminder-scheduler:local
TEST_DB_URL := mysql+pymysql://reminder_user:change_me@mysql:3306/reminder_scheduler_test

.PHONY: up down test demo \
        kind-up kind-deploy kind-status kind-logs kind-demo kind-down

# --- docker-compose: fast local iteration -------------------------------

up:
	docker compose up --build -d
	@echo "API: http://localhost:8000/docs"

down:
	docker compose down

test:
	docker compose run --rm -e TEST_DATABASE_URL="$(TEST_DB_URL)" api pytest -q

demo:
	docker compose run --rm api python -m scripts.seed_due_reminders

clean:
	docker compose down -v

# --- kind: the real thing -------------------------------------------------

kind-up:
	kind create cluster --name $(CLUSTER) --config k8s/kind-config.yaml
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/mysql-secret.yaml -f k8s/app-config.yaml -f k8s/mysql-init-configmap.yaml
	kubectl apply -f k8s/mysql.yaml
	kubectl -n reminder-scheduler rollout status deployment/mysql --timeout=120s
	kubectl apply -f k8s/api.yaml -f k8s/worker.yaml -f k8s/seed-cronjob.yaml
	kubectl -n reminder-scheduler rollout status deployment/api --timeout=90s
	kubectl -n reminder-scheduler rollout status deployment/worker --timeout=90s
	@echo "Cluster is up. API: http://localhost:8000/docs"

# Re-push the image and roll the deployments after a code change, without
# tearing the cluster down.
kind-deploy:
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)
	kubectl -n reminder-scheduler rollout restart deployment/api deployment/worker
	kubectl -n reminder-scheduler rollout status deployment/api --timeout=90s
	kubectl -n reminder-scheduler rollout status deployment/worker --timeout=90s

kind-status:
	kubectl -n reminder-scheduler get pods,deploy,cronjob

kind-logs:
	kubectl -n reminder-scheduler logs -l app=worker --tail=50 --prefix

kind-demo:
	kubectl -n reminder-scheduler create job --from=cronjob/seed-due-reminders seed-manual-$$(date +%s)
	@sleep 3
	kubectl -n reminder-scheduler get pods
	@echo "Watch it drain: curl -s localhost:8000/stats"

kind-down:
	kind delete cluster --name $(CLUSTER)
