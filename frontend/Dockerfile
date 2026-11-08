# Exradius admin panel: built with Vite, served by nginx, which proxies /api to the API
# so the session cookie stays same-origin. Unprivileged nginx (uid 101, port 8080), so the
# pod meets Pod Security "restricted".

FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY nginx.conf /etc/nginx/templates/default.conf.template
# Sourced by the image's entrypoint after 15-local-resolvers.envsh: the resolver for
# nginx.conf is EXRADIUS_RESOLVER when set, else the pod's nameservers.
COPY --chmod=755 <<'SCRIPT' /docker-entrypoint.d/16-exradius-resolver.envsh
#!/bin/sh
EXRADIUS_RESOLVER="${EXRADIUS_RESOLVER:-${NGINX_LOCAL_RESOLVERS:-127.0.0.11}}"
export EXRADIUS_RESOLVER
SCRIPT
COPY --from=build /app/dist /usr/share/nginx/html
# Where the API is, as nginx sees it: a fully qualified name (the chart sets it to the api
# Service's).
ENV EXRADIUS_API_UPSTREAM=http://exradius-api.exradius.svc.cluster.local:8000
# The image's own entrypoint step exports the nameservers of /etc/resolv.conf, which the
# step above uses unless EXRADIUS_RESOLVER is set.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
