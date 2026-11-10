# exradius-builder: the image of build pods (docs/kubernetes.md §12). Upstream rootless
# BuildKit, pinned, plus the Docker credential helpers with which a build pod signs in to a
# cloud registry as its own pod identity:
#
#   docker-credential-ecr-login      Amazon's, for ECR (EKS Pod Identity or IRSA)
#   docker-credential-exradius-gcr   builder/, for Artifact Registry (GKE Workload Identity)
#   docker-credential-exradius-acr   builder/, for ACR (AKS Workload Identity)
#
# values builds.credentialHelper (ecr, gcr, acr) names the one the operator writes into
# buildctl's Docker config for the registry of the image; with none (kind) they are unused.
# Build context: the repository root.

FROM --platform=$BUILDPLATFORM golang:1.27 AS build
ARG TARGETOS
ARG TARGETARCH
# amazon-ecr-credential-helper, from its Go module: `go get` checks it against the Go
# checksum database.
ARG ECR_LOGIN_VERSION=v0.12.0
WORKDIR /src
COPY builder/ ./
RUN set -eu; mkdir -p /out; \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" \
      -o /out/docker-credential-exradius .; \
    ln -s docker-credential-exradius /out/docker-credential-exradius-gcr; \
    ln -s docker-credential-exradius /out/docker-credential-exradius-acr
RUN set -eu; mkdir /ecr; cd /ecr; go mod init ecr-login >/dev/null 2>&1; \
    go get "github.com/awslabs/amazon-ecr-credential-helper/ecr-login@$ECR_LOGIN_VERSION"; \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" \
      -o /out/docker-credential-ecr-login \
      github.com/awslabs/amazon-ecr-credential-helper/ecr-login/cli/docker-credential-ecr-login

FROM moby/buildkit:v0.34.1-rootless@sha256:f68752a5a94acfcdad2d07867b7075601a421638e17fc5f28efc1c814d0c4f89
LABEL exradius.role="builder"
COPY --from=build /out/ /usr/local/bin/
