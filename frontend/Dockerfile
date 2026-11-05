# Exradius admin panel: built with Vite, served by nginx, which proxies /api to the API
# so the session cookie stays same-origin.

FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
# Where the API is, as nginx sees it. The compose service sets it to the api service.
ENV EXRADIUS_API_UPSTREAM=http://api:8000
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
