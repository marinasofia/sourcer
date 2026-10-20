> Current deployment: both repositories are private. The private workflow no
> longer publishes Pages or pushes generated data into the code repository.
> The cloud examples below require separate deployment validation.

# Deployment guide

Sourcer runs in five places. Pick the one that matches what you need; each
section is self-contained. The scheduled GitHub Actions run is the production
scheduler. Everything else is either a local surface or an alternative runtime
that has been verified but is not scheduled.

| Surface | Entry point | State | Cost cap default |
|---|---|---|---|
| Local CLI | `python -m sourcer.main` | `data/sourcer.db` | 2.50 USD |
| Docker Compose | `docker compose up --build` | `./data` volume | 2.50 USD |
| GitHub Actions (private repo) | `.github/workflows/refresh.yml` | `docs/data.json` committed | 0.01 USD |
| AWS Lambda | `sourcer.lambda_handler.handler` | S3 bucket | 1.00 USD |
| Kubernetes | `python -m sourcer.serve` | bundled `docs/data.json` | per request |

`SOURCER_COST_CAP` configures estimated admission limits for a pipeline run or
an HTTP/MCP scoring request, including corrective JSON retries. It does not
cap aggregate HTTP traffic or replace provider billing limits. These
examples require deployment-specific access controls and verification; see
[SECURITY.md](../SECURITY.md) before exposing a service.

## Local

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env                              # ANTHROPIC_API_KEY
cp skills_vector.example.json skills_vector.json  # edit the weights
SOURCER_COST_CAP=0.05 .venv/bin/python -m sourcer.main
```

`SOURCER_COST_CAP=0` runs the whole pipeline with zero model calls (fetch,
filter, dedupe, store). Useful for checking sources and rules for free.

## Docker Compose

```bash
ANTHROPIC_API_KEY=sk-ant-... docker compose up --build
```

The SQLite database persists in `./data` through the volume in
`docker-compose.yml`. `skills_vector.json` must exist before building because
the image copies it.

## GitHub Actions (scheduled)

The private data repository owns the schedule. Its workflow checks out this
public repository for code, seeds the private skills vector and database
export, runs the pipeline, commits the full results privately, and pushes
only `docs/data-public.json` and `docs/showcase.json` back here through a
deploy key. The `deploy-pages.yml` workflow in this repository then publishes
`docs/` to GitHub Pages.

Secrets on the private repository:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | model calls |
| `PUBLIC_DEPLOY_KEY` | SSH deploy key with write access to this repository |

## AWS Lambda

The full run fits inside Lambda's 15 minute limit. The function is a container
image (`Dockerfile.lambda`) with the SQLite database persisted to S3 between
invocations so dedupe keeps working.

Local smoke test with the runtime emulator baked into the base image, no AWS
account needed:

```bash
docker build -f Dockerfile.lambda -t sourcer-lambda .
docker run --rm -p 127.0.0.1:9000:8080 -e ANTHROPIC_API_KEY=... -e SOURCER_SKIP_S3=1 sourcer-lambda
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" -d '{}'
```

Deploy with the one-shot script after `aws configure`:

```bash
deploy/aws.sh
```

It creates the state bucket, ECR repository, IAM role, and function, or
updates the function code if it already exists. The EventBridge schedule is
deliberately not created because GitHub Actions is the one scheduler. Invoke
manually:

```bash
aws lambda invoke --function-name sourcer --cli-read-timeout 900 /dev/stdout
```

Before a production deployment, replace the `AmazonS3FullAccess` policy the
script attaches with a policy scoped to the single state bucket.

## Kubernetes (Minikube)

`sourcer/serve.py` is a stdlib-only HTTP scoring service with `GET /healthz`,
`GET /digest`, and `POST /score`. `k8s/deployment.yaml` runs it with one
replica, a liveness probe, and the API key and a bearer token from a Secret.
`k8s/service.yaml` keeps it inside the cluster (ClusterIP); reach it with
`kubectl port-forward`. The service will not start without `SOURCER_HTTP_TOKEN`,
even on loopback, because every `POST /score` is a paid model call and any web
page you visit can reach 127.0.0.1. `/digest` and `/score` need the token;
`SOURCER_COST_CAP` bounds the whole process's spend, at most
`SOURCER_HTTP_CONCURRENCY` (default 2) scores run at once, and each connection
gets 30 seconds in all.

```bash
minikube start --driver=docker
eval $(minikube docker-env)          # build straight into the cluster's daemon
docker build -t sourcer-serve:latest .
kubectl create secret generic sourcer-secrets --from-literal=anthropic-api-key=sk-ant-... \
  --from-literal=http-token="$(openssl rand -hex 24)"
kubectl apply -f k8s/
kubectl rollout status deployment/sourcer
kubectl port-forward svc/sourcer 8501:8500
curl localhost:8501/healthz
TOKEN=$(kubectl get secret sourcer-secrets -o jsonpath='{.data.http-token}' | base64 -d)
curl -XPOST localhost:8501/score -H "Authorization: Bearer $TOKEN" -d '{"posting_text": "..."}'
```

The Service is named `sourcer`, so Kubernetes injects `SOURCER_PORT=tcp://...`
into pods. The service port variable is therefore `SOURCER_HTTP_PORT`.

## Snowflake backend

Storage is selectable with `SOURCER_BACKEND` (default `sqlite`).
`sourcer/snowdb.py` mirrors the SQLite interface. Posting uniqueness uses
`MERGE` on the hash because Snowflake does not enforce `UNIQUE`.

```bash
pip install -e ".[snowflake]"
export SNOWFLAKE_ACCOUNT=abc12345.us-east-1 SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=...
python -m sourcer.snowdb check              # prints server version and row counts
SOURCER_BACKEND=snowflake python -m sourcer.main
```

## MCP server

```bash
python -m sourcer.mcp_server
```

Tools: `score_posting(posting_text, title, company)` parses and scores
arbitrary text (two model calls, about half a cent), `lookup_scored(company,
title_contains)` searches the local database for free.

```bash
claude mcp add sourcer -- /path/to/sourcer/.venv/bin/python -m sourcer.mcp_server
```
