# The egress proxy (docs/kubernetes.md §11). Run from the repository root with
# `make -C egress <target>`.

.PHONY: test vet fmt build

test:
	go test -race ./...

vet:
	go vet ./...

fmt:
	go fmt ./...

build:
	CGO_ENABLED=0 go build -trimpath -o bin/exradius-egress ./cmd
