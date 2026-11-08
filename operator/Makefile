# The Exradius operator (docs/kubernetes.md §5, §7). Run from the repository root with
# `make -C operator <target>`.

CONTROLLER_GEN_VERSION ?= v0.23.0
ENVTEST_VERSION ?= release-0.25
ENVTEST_K8S_VERSION ?= 1.36.2

LOCALBIN := $(CURDIR)/bin
CONTROLLER_GEN := $(LOCALBIN)/controller-gen
SETUP_ENVTEST := $(LOCALBIN)/setup-envtest
CRD_DIR := $(CURDIR)/../charts/exradius/files/crds

.PHONY: all generate manifests fmt vet test build tools

all: generate manifests build

$(LOCALBIN):
	mkdir -p $(LOCALBIN)

$(CONTROLLER_GEN): | $(LOCALBIN)
	GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_GEN_VERSION)

$(SETUP_ENVTEST): | $(LOCALBIN)
	GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-runtime/tools/setup-envtest@$(ENVTEST_VERSION)

tools: $(CONTROLLER_GEN) $(SETUP_ENVTEST)

# DeepCopy methods of the API types.
generate: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) object paths="./api/..."

# The CRD, as the chart ships it (charts/exradius/templates/crds.yaml includes the file).
manifests: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) crd paths="./api/..." output:crd:artifacts:config=$(CRD_DIR)

fmt:
	go fmt ./...

vet:
	go vet ./...

# Unit tests and envtest (a real kube-apiserver and etcd, no kubelet).
test: $(SETUP_ENVTEST)
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(LOCALBIN) -p path)" go test ./... -count=1

build:
	CGO_ENABLED=0 go build -o $(LOCALBIN)/exradius-operator ./cmd
