#!/bin/bash
set -e

echo "=== Installing NextCloud ==="
cd /var/www/nextcloud
rm -f config/config.php

sudo -u www-data php occ maintenance:install \
  --database "pgsql" \
  --database-name "nextcloud" \
  --database-host "localhost" \
  --database-user "nextclouduser" \
  --database-pass "nemo_sync_password_2026" \
  --admin-user "nemo_sync" \
  --admin-pass "nemo_sync_password_2026"

echo "=== Configuring Trusted Domains ==="
sudo -u www-data php occ config:system:set trusted_domains 1 --value="gyandev-nextcloud.duckdns.org"

echo "=== Enabling External Storage App ==="
sudo -u www-data php occ app:enable files_external

echo "=== Creating External Storage Mapping ==="
sudo -u www-data php occ files_external:create "Lab Files" local null \
  --config path="/srv/labdata/users/\$user" \
  --allow-sharing=true

echo "=== Set Perms on srv directories ==="
mkdir -p /srv/labdata/users /srv/labdata/groups /srv/labdata/public
setfacl -R -m d:u:www-data:rwx /srv/labdata/users
setfacl -R -m u:www-data:rwx /srv/labdata/users
setfacl -R -m d:u:www-data:rwx /srv/labdata/groups
setfacl -R -m u:www-data:rwx /srv/labdata/groups

echo "=== NextCloud Configured Successfully ==="
