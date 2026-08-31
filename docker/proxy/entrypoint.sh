#!/bin/sh
set -eu

envsubst '$WEB_DOMAIN' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

configuration_checksum() {
  sha256sum /etc/vertep/revocation/node-ca.crl /etc/vertep/tls/vertep.crt \
    | sha256sum \
    | awk '{print $1}'
}

last_checksum=$(configuration_checksum)
(
  while sleep 5; do
    current_checksum=$(configuration_checksum)
    if [ "$current_checksum" != "$last_checksum" ]; then
      nginx -s reload
      last_checksum=$current_checksum
    fi
  done
) &

exec nginx -g 'daemon off;'
