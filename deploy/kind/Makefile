# The local Kubernetes setup (docs/kubernetes.md §17). Run from anywhere:
#
#   make -C deploy/kind dev-up     # registry, kind cluster, operators, Secrets
#   tilt up                        # build, install and live-update (repository root)
#   make -C deploy/kind dev-down   # delete the cluster and the registry
#
#   make -C deploy/kind dev-seccomp  # the build pods' seccomp profile on the node (builds.seccompProfile)
#   make -C deploy/kind dev-gvisor   # gVisor (runsc) in the node and a RuntimeClass "gvisor" (runs.runtimeClassName)
#   make -C deploy/kind dev-vault    # Vault in dev mode (LOCAL ONLY) for stores of kind vault; then
#                                    # tilt up -- --vault, or install VAULT=1 (values-vault.yaml)
#
# Without Tilt: `make -C deploy/kind images install` builds every image with tag $(TAG),
# pushes it to the local registry and installs the chart with values-local.yaml (and
# values-vault.yaml with VAULT=1).
#
#   make -C deploy/kind e2e PROJECT=<slug> [AGENT=<slug>]
# publishes one task to the install and waits for its result (deploy/kind/e2e.py).

ROOT := $(abspath ../..)
CONTEXT := kind-exradius
REGISTRY := localhost:5001
TAG ?= 0.2.0
# The Python package version, which the runtime bundle checks (backend/pyproject.toml).
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(ROOT)/backend/pyproject.toml | head -1)

.PHONY: dev-up dev-down dev-seccomp dev-gvisor dev-vault images install uninstall e2e

dev-up:
	./dev-up.sh

dev-down:
	-kind delete cluster --name exradius
	-docker rm -f kind-registry

# charts/exradius/files/profiles/buildkit.json as /var/lib/kubelet/seccomp/exradius/buildkit.json on every
# node: values builds.seccompProfile: exradius/buildkit.json then runs build pods with it.
dev-seccomp:
	for node in $$(kind get nodes --name exradius); do \
	  docker exec $$node mkdir -p /var/lib/kubelet/seccomp/exradius && \
	  docker exec -i $$node cp /dev/stdin /var/lib/kubelet/seccomp/exradius/buildkit.json < $(ROOT)/charts/exradius/files/profiles/buildkit.json || exit 1; \
	  echo "$$node: /var/lib/kubelet/seccomp/exradius/buildkit.json"; \
	done

dev-gvisor:
	./dev-gvisor.sh

# Vault in dev mode in the cluster, its Kubernetes auth role for the api and the worker, and a
# sample secret (deploy/kind/dev-vault.sh). Local only. Run again after the Vault pod restarts.
dev-vault:
	./dev-vault.sh

images:
	docker build -f $(ROOT)/deploy/backend.Dockerfile -t $(REGISTRY)/exradius-backend:$(TAG) $(ROOT)
	docker build -t $(REGISTRY)/exradius-frontend:$(TAG) $(ROOT)/frontend
	docker build -f $(ROOT)/deploy/operator.Dockerfile -t $(REGISTRY)/exradius-operator:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/egress.Dockerfile -t $(REGISTRY)/exradius-egress:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/builder.Dockerfile -t $(REGISTRY)/exradius-builder:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/runtime-claude-code.Dockerfile --build-arg EXRADIUS_VERSION=$(VERSION) \
	  -t $(REGISTRY)/exradius-runtime-claude-code:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/project-default.Dockerfile -t $(REGISTRY)/exradius-project-default:$(TAG) $(ROOT)/deploy
	for image in backend frontend operator egress builder runtime-claude-code project-default; do \
	  docker push -q $(REGISTRY)/exradius-$$image:$(TAG) || exit 1; \
	done

install:
	helm upgrade --install exradius $(ROOT)/charts/exradius --kube-context $(CONTEXT) --namespace exradius \
	  -f $(ROOT)/charts/exradius/values-local.yaml $(if $(VAULT),-f $(ROOT)/deploy/kind/values-vault.yaml) \
	  --set images.tag=$(TAG)

uninstall:
	helm uninstall exradius --kube-context $(CONTEXT) --namespace exradius

e2e:
	@test -n "$(PROJECT)" || { echo "usage: make e2e PROJECT=<slug> [AGENT=<slug>]" >&2; exit 2; }
	cd $(ROOT)/backend && uv run python ../deploy/kind/e2e.py --project $(PROJECT) $(if $(AGENT),--agent $(AGENT))
