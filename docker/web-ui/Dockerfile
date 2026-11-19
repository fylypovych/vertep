# #504: the Vertep panel is its own artifact, deployed, restarted, updated and
# rolled back independently of CORE. It is a pure API client of CORE: state,
# auth/RBAC, approvals and orchestration stay on the backend.
FROM node:24-slim AS build
WORKDIR /app/web-v2
COPY web-v2/package.json web-v2/package-lock.json ./
RUN npm ci --silent
COPY web-v2/ .
RUN npm run build

FROM nginx:1.27-alpine
ARG VERTEP_VERSION=dev
ARG VERTEP_RELEASE_SHA=
COPY docker/web-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/web-v2/dist/vertep-admin-v2/browser /usr/share/nginx/html
# The panel reports which build it is (shown next to the CORE version, #504 AC3).
RUN printf '{"version":"%s","commit_sha":"%s","api_major":1}\n' "$VERTEP_VERSION" "$VERTEP_RELEASE_SHA" \
      > /usr/share/nginx/html/ui-version.json
EXPOSE 8080
