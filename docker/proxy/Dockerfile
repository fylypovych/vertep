FROM nginx:1.27-alpine
COPY deploy/proxy.conf.template /etc/nginx/conf.d/default.conf.template
COPY docker/proxy/entrypoint.sh /usr/local/bin/vertep-proxy-entrypoint
RUN chmod 0755 /usr/local/bin/vertep-proxy-entrypoint
