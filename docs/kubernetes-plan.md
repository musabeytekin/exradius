# Kubernetes: implementation phases

Status: agreed plan, 2026-11-08. Companion to `kubernetes.md`, which is the design. Each phase
is its own implementation session, conducted in English. Each lands on `main`, leaves a system
that installs and runs, rewrites the docs it makes true (`kubernetes.md` §20), and ends with a
live check done together with the user.

`v0.1.0` (924344b) is the last Docker-based version. Until phase 3, real projects (bank,
Outline, Saleor, Plane) run on it, and `main` runs only what the phases so far support. The
Kubernetes line is 0.2.0.

Order and why:

| # | Phase | Runs at the end |
|---|---|---|
| 1 | Runs on Kubernetes | image-based projects without services |
| 2 | Image builds and the registry | plus Dockerfile workstations |
| 3 | Services, resources, caches, Verify parity | the four real projects: parity with v0.1.0 |
| 4 | Egress proxy, NetworkPolicy, credential injection | the same, with no credentials in pods and an allowlist |
| 5 | Isolation: user namespaces, RuntimeClass | the same, hardened |
| 6 | Clouds: values files, storage backends, Terraform | the same on EKS, then GKE and AKS |

Builds come before services because every real project uses a workstation Dockerfile. Phase 1
proves the new architecture end to end on the smallest project that can exist.

## Phase 1: runs on Kubernetes

**Scope**

- `deploy/kind/` (cluster config, local registry, `make dev-up` / `dev-down`, operator installs
  for CloudNativePG and RabbitMQ) and the `Tiltfile`.
- `charts/exradius/`:
  - api with ports 8000 and 8001, worker, frontend, operator;
  - namespaces with Pod Security labels, ServiceAccounts and RBAC (`kubernetes.md` §3);
  - run identity ConfigMap;
  - CloudNativePG `Cluster`, `RabbitmqCluster`, SeaweedFS;
  - CRD templates;
  - `values.yaml` and `values-local.yaml`.
- `operator/` (kubebuilder):
  - the `Run` CRD, `v1alpha1`;
  - the pod builder: workspace PVC, `prepare`, `clone-*`, `setup-*`, `files`, workstation with
    the bundle as an image volume, security context as `kubernetes.md` §7.8;
  - status mapping with steps, log tails and image-pull failures;
  - `spec.cancel`, `spec.podGeneration`;
  - leader election.
- Worker:
  - `kube.py` (Runs and per-run Secrets, watch);
  - `RunManager` creates Runs, settles from Run status and the runner's reports, cancels,
    finalizes by deleting the Run;
  - recovery by listing Runs;
  - the `run_reported` control message and the 15 s sweep;
  - `worker.concurrency`;
  - credentials through the per-run Secret, as today.
- Runner:
  - waits in-process for answers (long-poll) and resumes in the same pod;
  - `prepare` and `clone` commands (git code moved from `worker/prepare.py`);
  - the delivery check after the session;
  - the `files` sidecar mode;
  - the hooks dispatcher from the bundle.
- api:
  - the internal app on 8001;
  - `/reported`, `/answers` long-poll, `/attachments/{id}`.
- `storage.py`: S3 against SeaweedFS. The filesystem backend becomes test-only.
- Verify:
  - "Read config", then a verify Run (prepare, clone, setup, list tools);
  - diagnosis rules for image pulls and unschedulable pods.
- Provider `oauth_token` probe as a probe Run.
- System status: worker heartbeat without Docker, plus operator presence.
- A project with a `dockerfile` or with `services` is not runnable yet. The health reason
  names it ("workstation.dockerfile needs image builds, not available yet"), so tasks are
  rejected with that reason and Verify stops at "Read config".
- `workstation.cache` is accepted and ignored with a warning.

**Replaces**

- `deploy/docker-compose.yml` and `.env.example`;
- `worker/containers.py`, `worker/bundles.py`, `worker/memory.py`;
- the run-directory half of `worker/prepare.py`;
- `delivery.py`'s container;
- `EXRADIUS_DATA_DIR`, `EXRADIUS_HOST_DATA_DIR`, `EXRADIUS_RUN_NETWORK`,
  `EXRADIUS_RUN_MEMORY_BUDGET`;
- `GET /api/system/images`;
- the Docker parts of `tests/test_integration.py` (replaced by `make e2e`).

**Docs rewritten:** README "Running it" and `CLAUDE.md` "Commands"; `architecture.md` §3, §5,
§10, §12 (runs, directories, recovery); `runtime-bundles.md` §2.2, §2.4, §2.5, §4.

**Done when**

- `uv run pytest` passes. Operator unit tests and envtest pass. `make dev-up && tilt up` brings
  a fresh cluster to a ready panel.
- A task on an image-based project goes `queued` → `preparing` → `running` → `completed`, with
  result, transcript and artifacts as on v0.1.0.
- A task that asks a question waits with its pod running, resumes in the same pod after the
  answer, and completes. Answer attachments are readable and read-only for the agent.
- Two tasks of the same project run in parallel.
- Cancel works in `preparing`, in `running` and in `waiting_for_input`.
- Deleting the worker pod during a run, and separately the operator pod, does not lose the run.
- Deleting a waiting run's pod by hand, then answering, recreates the pod on the same workspace
  and completes.
- After every run, `kubectl get pods,pvc,secrets -n exradius-runs` shows nothing of it.
- A run pod has no ServiceAccount token, no `*_SERVICE_HOST` variables, and a read-only root.

**Live check with the user**

- Create the kind cluster and install together, then add a small image-only config repository
  (the user picks or creates one) with a store and a provider.
- Send a task from the panel and answer its question.
- Watch `kubectl get runs,pods -n exradius-runs -w`.
- Cancel one task. Delete the worker pod during another.

## Phase 2: image builds and the registry

**Scope**

- The `Build` CRD and the operator's build controller: single-use rootless BuildKit pods in
  `exradius-builds`, registry cache, status with digest and log tail.
- Worker:
  - tags as today;
  - the context tar into the object store;
  - create-if-absent Build objects, waiting in `preparing`;
  - `image_builds` rows from Build status.
- The api's `/internal/builds/{id}/context`.
- The kind registry wired for pushes from pods and pulls by the node. `runs.imagePullSecrets`.
- The builder image (rootless BuildKit pinned; credential helpers come in phase 6).
- Verify "Build workstation" and "Build service <name>" steps.
- Lift the `dockerfile` restriction of phase 1 for the workstation. Services stay restricted.

**Replaces:** `worker/builds.py`'s Docker build and file lock.

**Docs rewritten:** `config-repo.md` §4.

**Done when**

- A project with a workstation Dockerfile Verifies, with the build log in the step, and runs a
  task.
- A second Verify at the same commit says the image is up to date without a build pod.
- Two runs started together at a new commit share one Build.
- A Dockerfile outside its context rebuilds when only the Dockerfile changes.
- A failing build fails the run in `preparing` with the log tail.
- Find out whether `exradius-builds` can run below `privileged` on kind (record the answer).

**Live check with the user:** the user pushes a change to a workstation Dockerfile in a small
config repository. Together, watch the auto-Verify build, then a task on the new image.

## Phase 3: services, resources, caches, Verify parity

**Scope**

- Services as native sidecars:
  - startup probes from `ready`;
  - HEALTHCHECK mapping from the image config;
  - `hostAliases`;
  - the security context;
  - service env from the per-run Secret;
  - `command`;
  - the operator's 120 s deadline.
- Service Dockerfiles (Build objects from phase 2).
- Config check: ports unique across services, `localhost` reserved.
- Resources: request = limit for declared memory; values defaults; CPU requests;
  `OOMKilled` diagnosis; "waiting for cluster capacity" with `runs.waiting_for = 'capacity'`.
- The project volume: mirrors and caches; values `runs.projectVolume`; deletion with the
  project.
- Verify parity:
  - "Start service" steps, setup with services, list tools against `localhost` services;
  - diagnosis rules for probe deadlines and OOM.
- The skill's check script mirrors the pod (services on the workstation's network,
  `network.allow` comes in phase 4); a skill release follows.

**Replaces**

- the rest of the Docker service code;
- the panel's cache size;
- the "Reserve memory", "Stop services" and "Remove directory" steps.

**Docs rewritten**

- `environment-and-services.md` §2;
- `run-environment.md` §4 and §5;
- `config-repo.md` §2 (ports, names, memory);
- `setup-and-verify.md`;
- `architecture.md` §14 (audit table, first half).

**Done when**

- bank (Maven + postgres), Outline, Saleor (two repositories, postgres and valkey) and Plane
  (four services) each Verify and complete a real task on kind.
- Plane's install time with a warm cache is about what it was on v0.1.0 (54 s).
- Two Plane runs in parallel each have their own services.
- A run whose declared memory is too small fails with "exceeded its declared memory".
- A run larger than the node waits as "waiting for cluster capacity" and starts when another
  run ends.
- A service that never becomes ready fails within 120 s with its log tail.
- compose and v0.1.0 are no longer needed for the user's projects.

**Live check with the user**

- Re-add the four config repositories to the kind install; the user fills the stores.
- Verify all four, run one real ticket on each, and compare timings with the v0.1.0 numbers in
  the live-test notes.
- The Jira bridge (exradius-jira) is pointed at the kind install's RabbitMQ and API through the
  port-forwards, to show that the contract is unchanged.

## Phase 4: egress proxy, NetworkPolicy, credential injection

**Scope**

- `egress/` (Go):
  - forward proxy with host decisions on `CONNECT`;
  - injecting reverse proxy for the model, git and HTTP connections;
  - pod-IP identity and phase from the pod watch;
  - upstream proxy chaining;
  - audit batching.
- The api's `/internal/egress/runs/{id}` and records endpoint, authenticated by TokenReview;
  the `egress_records` table; hosts reached and refused on the task page.
- Proxy variables and `JAVA_TOOL_OPTIONS` in run pods.
- `ANTHROPIC_BASE_URL` with placeholders; git `insteadOf` per repository.
- The runner points HTTP connections at the proxy.
- No provider credential and no repository credential in the per-run Secret any more.
- Schema and environment section:
  - `network.allow` in the schema;
  - `hosts` on catalog entries;
  - values `egress.allow`;
  - the environment section's network line;
  - the skill writes `network.allow`.
- NetworkPolicies for run, build, api and egress pods.

**Replaces:** `EXRADIUS_GIT_CREDENTIAL_<N>` and the push credential helper; provider variables
in pods; the "Exradius's own egress proxy" entry in `ideas.md`.

**Docs rewritten**

- `architecture.md` §9 and §14 (second half);
- `environment-and-services.md` §1;
- `config-repo.md` §2 (`network.allow`);
- `run-environment.md` §2.1;
- `ideas.md`.

**Done when**

- From a run's shell, a direct connection to the internet, the Kubernetes API, Exradius's
  Postgres, RabbitMQ and another run's pod all fail.
- Through the proxy, a host not on the list is refused in the agent phase, with a `403` that
  names it, and allowed during setup.
- `env` in the agent's shell shows no model key and no git token. Clone, fetch and push to a
  `push: true` repository work; a push to a `push: false` one is refused by the proxy.
- An `api_key` provider and an `oauth_token` provider both work through `ANTHROPIC_BASE_URL`.
  This is the main unverified assumption of the design.
- An HTTP MCP connection with a secret header works without the header in the pod.
- bank's Maven build works through the proxy, or the needed `settings.xml` is in its
  workstation image and the skill knows the pitfall.
- The four projects still Verify and run. Their `network.allow` lists are written.

**Live check with the user**

- The user sends a task that tries a host not on the list and sees the refusal, in the result
  and on the task page.
- Inspect a run's environment together.
- Push from a `push: false` repository.

## Phase 5: isolation

**Scope**

- `runs.userNamespaces` (`hostUsers: false`) for run pods, and for build pods if rootless
  BuildKit then runs at `baseline`.
- `runs.runtimeClassName`, and `make dev-gvisor` (runsc in the kind node, a `gvisor`
  RuntimeClass).
- Pod Security review of all three namespaces.
- A written decision on agent-sandbox warm pools (`kubernetes.md` §22): adopt with a design
  note, or close.

**Done when**

- With user namespaces, `/proc/self/uid_map` in a run shows a mapped range, services that start
  as root still work, and the four projects Verify and run.
- With gVisor on kind (if `runsc` works on this Mac's arm64 kind node; otherwise recorded as not
  possible locally), the four projects Verify, or each failure is recorded with its cause. The
  slowdown of Plane's install is measured.
- `exradius-builds` runs at the lowest Pod Security level that works, recorded.

**Live check with the user:** Verify and one task per project with each isolation setting,
then decide the defaults per environment together.

## Phase 6: clouds

**Scope**

- `storage.py` `gcs` and `azure` backends with pod identity.
- Builder credential helpers (ECR, GCR/Artifact Registry, ACR).
- `values-eks.yaml`, `values-gke.yaml`, `values-aks.yaml`.
- `deploy/terraform/aws` first, then `gcp`, then `azure`:
  - cluster with system and run node pools;
  - registry, bucket, managed Postgres;
  - Amazon MQ on AWS, the RabbitMQ operator elsewhere;
  - optional RWX storage, workload identity, a generated values file.
- Ingress and `publicApiUrl`.
- Per cloud, check:
  - image volume support on the managed node image, or `runs.bundleMount: copy`;
  - user namespaces;
  - RuntimeClass offerings (expected: GKE Sandbox's gVisor and AKS Pod Sandboxing's Kata;
    verify);
  - NetworkPolicy enforcement.

**Done when**

- `terraform apply` and `helm install -f <generated values>` take an empty AWS account to a
  completed task on a real project. Then the same for GCP and Azure.
- Only the values file differs between kind and each cloud.
- The panel, the bridges and the CLI work against the cloud install.

**Live check with the user:** in the user's own cloud account (it costs money; the user decides
which cloud and when), from `terraform apply` to a real ticket and `terraform destroy`.
