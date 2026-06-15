#!/bin/bash
set -e

echo "=== Creating External Storage Mapping ==="
cd /var/www/nextcloud
sudo -u www-data php occ files_external:create "Lab Files" local null::null \
  --config datadir="/srv/labdata/users/\$user"

echo "=== Setting Directory Permissions for www-data ==="
mkdir -p /srv/labdata/users /srv/labdata/groups /srv/labdata/public
setfacl -R -m d:u:www-data:rwx /srv/labdata/users
setfacl -R -m u:www-data:rwx /srv/labdata/users
setfacl -R -m d:u:www-data:rwx /srv/labdata/groups
setfacl -R -m u:www-data:rwx /srv/labdata/groups

echo "=== Finalization Completed Successfully ==="
