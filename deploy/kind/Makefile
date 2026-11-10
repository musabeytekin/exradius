# The local Kubernetes setup (docs/kubernetes.md §17). Run from anywhere:
#
#   make -C deploy/kind dev-up     # registry, kind cluster, operators, Secrets
#   tilt up                        # build, install and live-update (repository root)
#   make -C deploy/kind dev-down   # delete the cluster and the registry
#
# Without Tilt: `make -C deploy/kind images install` builds every image with tag $(TAG),
# pushes it to the local registry and installs the chart with values-local.yaml.
#
#   make -C deploy/kind e2e PROJECT=<slug> [AGENT=<slug>]
# publishes one task to the install and waits for its result (deploy/kind/e2e.py).

ROOT := $(abspath ../..)
CONTEXT := kind-exradius
REGISTRY := localhost:5001
TAG ?= 0.2.0
# The Python package version, which the runtime bundle checks (backend/pyproject.toml).
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(ROOT)/backend/pyproject.toml | head -1)

.PHONY: dev-up dev-down images install uninstall e2e

dev-up:
	./dev-up.sh

dev-down:
	-kind delete cluster --name exradius
	-docker rm -f kind-registry

images:
	docker build -f $(ROOT)/deploy/backend.Dockerfile -t $(REGISTRY)/exradius-backend:$(TAG) $(ROOT)
	docker build -t $(REGISTRY)/exradius-frontend:$(TAG) $(ROOT)/frontend
	docker build -f $(ROOT)/deploy/operator.Dockerfile -t $(REGISTRY)/exradius-operator:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/egress.Dockerfile -t $(REGISTRY)/exradius-egress:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/runtime-claude-code.Dockerfile --build-arg EXRADIUS_VERSION=$(VERSION) \
	  -t $(REGISTRY)/exradius-runtime-claude-code:$(TAG) $(ROOT)
	docker build -f $(ROOT)/deploy/project-default.Dockerfile -t $(REGISTRY)/exradius-project-default:$(TAG) $(ROOT)/deploy
	for image in backend frontend operator egress runtime-claude-code project-default; do \
	  docker push -q $(REGISTRY)/exradius-$$image:$(TAG) || exit 1; \
	done

install:
	helm upgrade --install exradius $(ROOT)/charts/exradius --kube-context $(CONTEXT) --namespace exradius \
	  -f $(ROOT)/charts/exradius/values-local.yaml --set images.tag=$(TAG)

uninstall:
	helm uninstall exradius --kube-context $(CONTEXT) --namespace exradius

e2e:
	@test -n "$(PROJECT)" || { echo "usage: make e2e PROJECT=<slug> [AGENT=<slug>]" >&2; exit 2; }
	cd $(ROOT)/backend && uv run python ../deploy/kind/e2e.py --project $(PROJECT) $(if $(AGENT),--agent $(AGENT))
