#!/bin/bash
set -e

echo "=== Creating Database ==="
sudo -u postgres psql -c "CREATE DATABASE nextcloud;" || true
sudo -u postgres psql -c "CREATE USER nextclouduser WITH PASSWORD 'nemo_sync_password_2026';" || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE nextcloud TO nextclouduser;" || true
sudo -u postgres psql -c "ALTER DATABASE nextcloud OWNER TO nextclouduser;" || true

echo "=== Downloading NextCloud ==="
cd /var/www
if [ ! -d "nextcloud" ]; then
    wget -q https://download.nextcloud.com/server/releases/latest.zip
    unzip -q latest.zip
    chown -R www-data:www-data nextcloud
    rm latest.zip
fi

echo "=== Configuring Firewall ==="
ufw allow 80/tcp || true
ufw allow 443/tcp || true

echo "=== Configuring Temporary Nginx for Port 80 ==="
cat << 'EOF' > /etc/nginx/sites-available/nextcloud
server {
    listen 80;
    listen [::]:80;
    server_name gyandev-nextcloud.duckdns.org;
    root /var/www/nextcloud/;
    index index.php index.html;
    location / {
        try_files $uri $uri/ /index.php$request_uri;
    }
}
EOF
rm -f /etc/nginx/sites-enabled/default || true
if [ ! -f "/etc/nginx/sites-enabled/nextcloud" ]; then
    ln -s /etc/nginx/sites-available/nextcloud /etc/nginx/sites-enabled/nextcloud
fi
systemctl restart nginx

echo "=== Getting SSL Certificate ==="
certbot --nginx -d gyandev-nextcloud.duckdns.org --non-interactive --agree-tos --register-unsafely-without-email

echo "=== Configuring Full Nginx SSL Site ==="
cat << 'EOF' > /etc/nginx/sites-available/nextcloud
server {
    listen 80;
    listen [::]:80;
    server_name gyandev-nextcloud.duckdns.org;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name gyandev-nextcloud.duckdns.org;

    ssl_certificate /etc/letsencrypt/live/gyandev-nextcloud.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gyandev-nextcloud.duckdns.org/privkey.pem;

    root /var/www/nextcloud/;

    client_max_body_size 0;
    fastcgi_request_buffering off;
    proxy_request_buffering off;

    index index.php index.html;

    location / {
        rewrite ^ /index.php$request_uri;
    }

    location ~ ^\/(?:index|remote|public|cron|core\/ajax\/update|status|ocs\/v[12]|signer|pathtoyourfiles)\.php(?:$|\/) {
        fastcgi_split_path_info ^(.+?\.php)(\/.*)$;
        set $path_info $fastcgi_path_info;
        try_files $fastcgi_script_name =404;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param PATH_INFO $path_info;
        fastcgi_param HTTPS on;
        fastcgi_pass unix:/run/php/php-fpm.sock;
    }

    location ~* \.(?:css|js|woff2?|svg|gif|map)$ {
        try_files $uri /index.php$request_uri;
        add_header Cache-Control "public, max-age=15778463";
    }
}
EOF

systemctl restart nginx

echo "=== Installing NextCloud via OCC ==="
cd /var/www/nextcloud
if ! sudo -u www-data php occ status >/dev/null 2>&1; then
    sudo -u www-data php occ maintenance:install \
      --database "pgsql" \
      --database-name "nextcloud" \
      --database-host "localhost" \
      --database-user "nextclouduser" \
      --database-pass "nemo_sync_password_2026" \
      --admin-user "nemo_sync" \
      --admin-pass "nemo_sync_password_2026"
fi

echo "=== Configuring NextCloud settings ==="
sudo -u www-data php occ config:system:set trusted_domains 1 --value="gyandev-nextcloud.duckdns.org" || true
sudo -u www-data php occ app:enable files_external || true

echo "=== Creating External Storage Mount ==="
# Map `/srv/labdata/users/$user`
sudo -u www-data php occ files_external:create "Lab Files" local null \
  --config path="/srv/labdata/users/\$user" \
  --allow-sharing=true || true

echo "=== Setting Directory Permissions for www-data ==="
mkdir -p /srv/labdata/users /srv/labdata/groups /srv/labdata/public
setfacl -R -m d:u:www-data:rwx /srv/labdata/users || true
setfacl -R -m u:www-data:rwx /srv/labdata/users || true
setfacl -R -m d:u:www-data:rwx /srv/labdata/groups || true
setfacl -R -m u:www-data:rwx /srv/labdata/groups || true

echo "=== NextCloud Installation Completed Successfully ==="
