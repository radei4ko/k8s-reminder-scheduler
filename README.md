# K8s Reminder Scheduler

[![tests](https://github.com/radei4ko/k8s-reminder-scheduler/actions/workflows/tests.yml/badge.svg)](https://github.com/radei4ko/k8s-reminder-scheduler/actions/workflows/tests.yml)

A Kubernetes-native job scheduler for delinquency reminders: several worker
pods claim tasks from a shared MySQL table, send a reminder, and guarantee
each one gets sent exactly once even when a pod is killed mid-send.

Built with FastAPI, SQLAlchemy, MySQL 8, and plain Kubernetes primitives - a
Deployment for the workers, a CronJob to generate new reminders, no queue
broker in between.

This is the companion piece to
[payment-retry-engine](https://github.com/radei4ko/payment-retry-engine): that
project retries a payment against a gateway from a single process; this one
takes the same "never process a task twice" problem and asks what changes
when the work is spread across several pods that can be killed at any moment
by a rollout, a scale-down, or a node eviction. The answer is a different
locking strategy, and that difference is the actual content of this repo.

---

## Why the locking is different from a single-process retry engine

payment-retry-engine holds a MySQL row lock for the entire duration of a
charge attempt: `SELECT ... FOR UPDATE`, call the gateway, write the result,
commit. That is fine when the call is fast and synchronous.

A reminder send is the same shape of external call, but here it is made
across a fleet of pods that Kubernetes can terminate without warning. Holding
a database lock open for the duration of an HTTP call to a notification
provider - one that can hang, retry internally, or just be slow - blocks
replication and every other pod's claim query for as long as that call takes.
That is how a flaky third-party API turns into a database incident.

So the lock here is held only for the instant it takes to *claim* a task, not
for the send itself:

```
   claim (short transaction,        send (no lock held -           report
   lock released on commit)         this can take a while,         (short
        │                           hang, or the pod can            transaction,
        │                           die here)                       fenced)
        v                                  v                            v
  ┌───────────┐                    ┌──────────────┐            ┌───────────────┐
  │ SKIP LOCKED│ ──lease acquired──>│ notifier.send │──result──>│ complete_task  │
  │ + UPDATE   │                    │  (no lock)    │           │ (checked against│
  └───────────┘                    └──────────────┘            │  lease_token)  │
                                                                  └───────────────┘
```

Between claim and report, nothing in the database says "this pod is working
on this task" except a row with `status=leased`, an `owner_id`, and a
`lease_expires_at` timestamp - no lock, nothing that a crashed pod could leave
stuck. If the pod dies in the middle, the row just sits there until the lease
expires, and any other pod's next claim query picks it back up.

---

## The state machine

```
    PENDING ──claimed by a worker──> LEASED ──sent ok──────────> DONE
       ^                               │  │
       │                               │  └──lease expired,
       │                               │     nobody completed it──┐
       │                               │                          │
       │                               └──send failed, retriable──┼──> RETRY_SCHEDULED
       │                                                          │           │
       └──────────────────────────────────────────────────────────┘           │
                     back to PENDING once the lease-expiry sweep reclaims it   │
                                                                                │
                     attempts exhausted, or a non-retriable failure            │
                     (invalid_recipient, unsubscribed)                        │
                                       v                                      │
                                     DEAD <───────────────backoff elapsed──────┘
```

`LEASED` is reachable from both a fresh claim and a reclaimed one - there is
no separate "abandoned" state. A pod that finds an expired lease treats it
exactly like a fresh `PENDING` task: it re-leases it to itself and tries
again, first writing a `task_attempts` row with `error_code=lease_expired`
attributed to whichever pod held it before, so the abandonment is visible in
the history rather than silently overwritten.

---

## Quick start

### Fast path: docker-compose

```bash
cp .env.example .env && make up
```

API on `http://localhost:8000/docs`, two worker containers running against
one MySQL. Seed some reminders and watch them get claimed:

```bash
make demo
curl -s localhost:8000/stats
```

### The real thing: a local Kubernetes cluster with kind

```bash
make kind-up      # creates the cluster, builds the image, loads it, applies everything
make kind-status   # pods, deployments, cronjob
curl -s localhost:8000/stats   # no port-forward needed - see k8s/kind-config.yaml
make kind-demo     # triggers the CronJob's Job right now instead of waiting up to 5 minutes
make kind-logs     # tail all three worker pods, prefixed by pod name
make kind-down     # tear down the cluster
```

`kind-up` deploys: MySQL (Deployment + PVC), the API (Deployment + NodePort
Service), three worker replicas (Deployment), and a CronJob that seeds new
reminders every five minutes - the same five files in `k8s/` that `kubectl
apply -f k8s/` would apply directly.

---

## Proof: killing a pod mid-task does not lose or duplicate the reminder

This is not a simulated test run - it is an actual session against the kind
cluster from `make kind-up`, worker replicas scaled to zero so a throwaway
pod could claim the task deterministically instead of racing the real ones.

**1. Queue a reminder, claim it from a pod, and leave it hanging** - as if the
pod had frozen right after taking the lease:

```
$ curl -s localhost:8000/reminders/7
{"status": "leased", "owner_id": "crash-sim-doomed", "attempts_made": 1, ...}
```

**2. Kill that pod the way a node eviction or an OOM kill would - no SIGTERM,
no graceful shutdown:**

```bash
kubectl delete pod crash-sim --grace-period=0 --force
```

**3. Scale the real worker Deployment back up and wait past the 30-second
lease:**

```
$ curl -s localhost:8000/reminders/7
{
  "status": "done",
  "attempts_made": 2,
  "attempts": [
    {"attempt_number": 1, "worker_id": "crash-sim-doomed",        "succeeded": false, "error_code": "lease_expired"},
    {"attempt_number": 2, "worker_id": "worker-76c6f48c74-rmdhs", "succeeded": true,  "error_code": null}
  ]
}
```

A different, real pod (`worker-76c6f48c74-rmdhs`) picked up the abandoned
lease and finished the job, and the history shows exactly what happened to
the first attempt instead of just quietly losing it.

---

## API

| Method | Path | What it does |
|---|---|---|
| `POST` | `/reminders` | Queue a reminder. Idempotent - `201` on create, `200` if the key was seen before. |
| `GET` | `/reminders` | List, filterable by `loan_id` and `status`. |
| `GET` | `/reminders/{id}` | One task plus its full attempt history. |
| `GET` | `/stats` | Counts per status, and how many tasks are claimable right now. |
| `GET` | `/healthz` | Liveness - actually touches the database. |

Sending is not on this API at all - it happens in the worker pods
(`app/worker.py`), which is the point: the API can be scaled, restarted, or
taken down without affecting reminders already in flight.

---

## Design decisions

### The fencing token, and why owner_id alone is not enough

A lease being reclaimed does not mean the original pod is actually dead - it
might just be slow, stuck behind a GC pause or a hung TCP connection, and
still convinced it owns the task. If it later wakes up and reports a result,
that result has to be rejected, or it can silently overwrite whatever the pod
that rescued the task already did.

`lease_token` is a fresh random value written on every claim, including a
reclaim. `complete_task` only accepts a result if the token presented matches
what is currently on the row:

```python
if task is None or task.status != TaskStatus.LEASED or task.lease_token != lease_token:
    return None  # this pod no longer owns the task - drop the result
```

`test_fencing.py` walks through exactly this: pod A claims, its lease
expires, pod B reclaims and completes, and only then does A's late result
arrive. It is rejected, and B's completion stands untouched.

One thing the fencing token does *not* do: guarantee the notification is sent
only once. It stops the *database* from recording two completions, but if A
was not actually dead - just slow - both A and B might genuinely call the
notifier for the same task before A's result gets rejected. That is why the
notifier call is keyed by `{task_id}:{attempt_number}` (see
`app/notifier.py`): true exactly-once delivery has to be enforced by the
notification provider's own idempotency key, the same way it is in
payment-retry-engine's gateway calls. The fencing token guarantees exactly-once
*bookkeeping*; provider-side idempotency is what gets you exactly-once
*delivery*.

### Every worker replica gets its identity for free

`owner_id` needs to be unique per pod, or the database cannot tell two
workers apart when something needs debugging. Rather than wiring the pod name
through the downward API, `app/config.py` falls back to
`socket.gethostname()` - which Kubernetes already sets to the pod's name for
every container, no extra manifest configuration required. Under
docker-compose it resolves to the container's own hostname just the same, so
local and cluster runs behave identically. See `k8s/worker.yaml`'s comment for
the full reasoning - this replaced an earlier version of `docker-compose.yml`
that gave every replica the *same* `WORKER_ID`, which defeated the entire
point of being able to tell pods apart.

### Two commits are honest bug fixes, kept as separate commits

- `synchronize_session=False` on the claim's bulk `UPDATE` left a stale copy
  of the task in the ORM's identity map, so a session that claimed and then
  completed a task in the same request - exactly what the test suite does -
  saw pre-update data and fenced out its own legitimate completion. Fixed
  with `synchronize_session="fetch"`, and left in the commit history rather
  than squashed away, the same way payment-retry-engine's concurrency bug
  is.
- The `WORKER_ID` duplication above.

Neither surfaced in casual manual testing - both were caught by the test
suite and by actually running the thing in kind, which is the argument for
having both.

### Same idempotency approach as payment-retry-engine

A unique index on `idempotency_key`, `IntegrityError` treated as an expected
outcome rather than an error, and a same-key-different-payload request
rejected with `409` instead of silently returning the wrong task. See that
project's README for the full reasoning; it is unchanged here.

---

## Tests

```bash
make test
```

26 tests against a real MySQL 8 - `SKIP LOCKED` and lease-expiry timing are
exactly what needs a real database, not SQLite standing in for one.

| File | Covers |
|---|---|
| `test_idempotency.py` | Duplicate keys, conflicting reuse, the insert race |
| `test_lease_lifecycle.py` | Claim, complete, retry scheduling, exhaustion |
| `test_lease_expiry.py` | Reclaiming an abandoned lease, unexpired leases left alone |
| `test_fencing.py` | A stale completion after reclaim is rejected, not applied |
| `test_concurrency.py` | Four real threads, forty tasks, zero double-sends |
| `test_retry_policy.py` | Backoff growth and its cap |
| `test_api.py` | Status codes, validation, filtering |

CI runs the full suite plus ten repetitions of the concurrency, fencing, and
lease-expiry tests specifically - races are non-deterministic, and a single
green run of a race test proves very little.

---

## Deliberately left out

- **Schema via `create_all`, not migrations.** Same tradeoff as
  payment-retry-engine - fine for a demo, Alembic is the real answer.
- **The notifier is a stub.** No real email/SMS provider integration.
- **MySQL is a single-replica Deployment**, not a managed database or an
  operator-managed StatefulSet with real replication.
- **Secrets are a plain `Secret` manifest with a demo password committed to
  git** - acceptable for a cluster nobody else can reach; `k8s/mysql-secret.yaml`
  says what the real answer is (Sealed Secrets, External Secrets Operator,
  Vault).
- **No authentication on the API**, no NetworkPolicy, no resource
  requests/limits on the pods, no HorizontalPodAutoscaler.
- **No dead-letter alerting.** A task that goes `DEAD` just sits there;
  paging someone is out of scope for this demo but would be the obvious next
  step.
