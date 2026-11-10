#!/usr/bin/env bash
# gVisor in the local kind cluster (docs/kubernetes.md §7.8, §17): runsc and its containerd
# shim in every node, a containerd runtime "runsc", and the RuntimeClass "gvisor". Run pods
# use it with values runs.runtimeClassName: gvisor. Idempotent.
#
# gVisor is a user-space kernel: a run's processes talk to it instead of the node's kernel.
# The release is pinned and checked against the sha512 published beside it.
set -euo pipefail

cluster=exradius
context=kind-$cluster
version=20261026.0
sha512_aarch64=da177d9bcfd9ac23899ecfedc6fd47358e9684a1f7e3f5f8ffb8bf7c0ca65f4c08730c0c503b9231b522db28b04c132a06d04d6719e0d8c04c9f82edaf03d8ae
sha512_x86_64=26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a

say() { printf '==> %s\n' "$*"; }
sha512() { if command -v sha512sum >/dev/null; then sha512sum "$1"; else shasum -a 512 "$1"; fi | cut -d' ' -f1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for node in $(kind get nodes --name "$cluster"); do
  arch=$(docker exec "$node" uname -m)
  case "$arch" in
    aarch64) want=$sha512_aarch64 ;;
    x86_64) want=$sha512_x86_64 ;;
    *) echo "gVisor has no release for $arch" >&2; exit 1 ;;
  esac
  archive="$work/gvisor-$arch.tar.bz2"
  if [ ! -f "$archive" ]; then
    say "gVisor $version for $arch"
    curl -fsSL -o "$archive" "https://storage.googleapis.com/gvisor/releases/release/$version/$arch/gvisor.tar.bz2"
    if [ "$(sha512 "$archive")" != "$want" ]; then
      echo "gVisor $version for $arch: checksum mismatch" >&2
      exit 1
    fi
    mkdir -p "$work/$arch"
    tar -xjf "$archive" -C "$work/$arch"
  fi

  say "runsc on $node"
  # runsc, its containerd shim, and gvisor-bin/ (the sentry and helpers runsc starts).
  # COPYFILE_DISABLE: macOS tar would add AppleDouble ._* files.
  COPYFILE_DISABLE=1 tar -C "$work/$arch" -cf - . | docker exec -i "$node" tar -xf - --no-same-owner -C /usr/local/bin
  # kind's kubelet uses the systemd cgroup driver, so runsc must read systemd cgroup paths.
  printf '[runsc_config]\n  systemd-cgroup = "true"\n' | docker exec -i "$node" cp /dev/stdin /etc/containerd/runsc.toml
  if ! docker exec "$node" grep -q 'runtimes.runsc]' /etc/containerd/config.toml; then
    docker exec -i "$node" sh -c 'cat >> /etc/containerd/config.toml' <<'EOF'

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
EOF
    # Running containers keep running: their shims outlive containerd.
    docker exec "$node" systemctl restart containerd
  fi
  docker exec "$node" /usr/local/bin/runsc --version | head -1
done

say "RuntimeClass gvisor"
kubectl --context "$context" apply -f - >/dev/null <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
EOF
say "ready: runs.runtimeClassName: gvisor"
