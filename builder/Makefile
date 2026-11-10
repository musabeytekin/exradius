# The builder image's credential helper (docs/kubernetes.md §12). Run from the repository
# root with `make -C builder <target>`.

.PHONY: test vet fmt build

test:
	go test -race ./...

vet:
	go vet ./...

fmt:
	go fmt ./...

build:
	CGO_ENABLED=0 go build -trimpath -o bin/docker-credential-exradius .
