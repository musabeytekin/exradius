# exradius-operator: turns Run objects into pods (docs/kubernetes.md §5, §7).
# Build context: the repository root.

FROM --platform=$BUILDPLATFORM golang:1.27 AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY operator/go.mod operator/go.sum ./
RUN go mod download
COPY operator/ ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /exradius-operator ./cmd

FROM gcr.io/distroless/static:nonroot
LABEL exradius.role="operator"
COPY --from=build /exradius-operator /exradius-operator
USER 65532:65532
ENTRYPOINT ["/exradius-operator"]
