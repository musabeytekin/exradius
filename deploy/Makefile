# Images and bundles outside Tilt. Run from the repository root: make -C deploy <target>
#
#   make -C deploy push REGISTRY=<registry> [TAG=0.2.0] [PLATFORM=linux/arm64]
# builds every Exradius image for PLATFORM and pushes it to REGISTRY, as a cloud install's
# values name it (images.registry; deploy/terraform's output `push_command` says which).
# Sign in to the registry first (aws ecr get-login-password | docker login, gcloud auth
# configure-docker, az acr login). PLATFORM is the nodes' architecture: arm64 for the
# Graviton, Axion and Cobalt node pools the Terraform modules make by default.

ROOT := $(abspath ..)
TAG ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(ROOT)/backend/pyproject.toml | head -1)
PLATFORM ?= linux/arm64
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(ROOT)/backend/pyproject.toml | head -1)

.PHONY: bundle-claude-code push

# The Claude Code runtime bundle directory, in deploy/build/runtime-claude-code/
bundle-claude-code:
	rm -rf build/runtime-claude-code
	docker build -f runtime-claude-code.Dockerfile --target bundle \
	  --output type=local,dest=build/runtime-claude-code $(ROOT)

push:
	@test -n "$(REGISTRY)" || { echo "usage: make -C deploy push REGISTRY=<registry> [TAG=$(TAG)] [PLATFORM=$(PLATFORM)]" >&2; exit 2; }
	docker buildx build --platform $(PLATFORM) --push -f backend.Dockerfile -t $(REGISTRY)/exradius-backend:$(TAG) $(ROOT)
	docker buildx build --platform $(PLATFORM) --push -t $(REGISTRY)/exradius-frontend:$(TAG) $(ROOT)/frontend
	docker buildx build --platform $(PLATFORM) --push -f operator.Dockerfile -t $(REGISTRY)/exradius-operator:$(TAG) $(ROOT)
	docker buildx build --platform $(PLATFORM) --push -f egress.Dockerfile -t $(REGISTRY)/exradius-egress:$(TAG) $(ROOT)
	docker buildx build --platform $(PLATFORM) --push -f builder.Dockerfile -t $(REGISTRY)/exradius-builder:$(TAG) $(ROOT)
	docker buildx build --platform $(PLATFORM) --push -f runtime-claude-code.Dockerfile \
	  --build-arg EXRADIUS_VERSION=$(VERSION) -t $(REGISTRY)/exradius-runtime-claude-code:$(TAG) $(ROOT)
	docker buildx build --platform $(PLATFORM) --push -f project-default.Dockerfile \
	  -t $(REGISTRY)/exradius-project-default:$(TAG) $(ROOT)/deploy
