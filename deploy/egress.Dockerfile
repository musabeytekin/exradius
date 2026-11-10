# exradius-egress: the egress proxy of run and build pods (docs/kubernetes.md §11).
# Build context: the repository root. The module has no dependencies but the standard library.

FROM --platform=$BUILDPLATFORM golang:1.27 AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY egress/go.mod ./
COPY egress/ ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /exradius-egress ./cmd

FROM gcr.io/distroless/static:nonroot
LABEL exradius.role="egress"
COPY --from=build /exradius-egress /exradius-egress
USER 65532:65532
ENTRYPOINT ["/exradius-egress"]
