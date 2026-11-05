# exradius-project-default: the image of run containers for projects that name no image
# (docs/runtime-bundles.md §2.3). A default, not a base: project images can be any
# glibc-based Linux image with git; nothing needs to inherit from this one.

FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*
