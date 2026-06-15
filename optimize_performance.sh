#!/bin/bash
set -e

echo "=== Optimizing PHP 8.3 FPM Settings ==="
PHP_INI="/etc/php/8.3/fpm/php.ini"
# Update existing values if present
sed -i 's/^upload_max_filesize =.*/upload_max_filesize = 100G/' $PHP_INI || true
sed -i 's/^post_max_size =.*/post_max_size = 100G/' $PHP_INI || true
sed -i 's/^memory_limit =.*/memory_limit = 1024M/' $PHP_INI || true
sed -i 's/^max_execution_time =.*/max_execution_time = 86400/' $PHP_INI || true
sed -i 's/^max_input_time =.*/max_input_time = 86400/' $PHP_INI || true
sed -i 's/^default_socket_timeout =.*/default_socket_timeout = 86400/' $PHP_INI || true

# Append values just in case they were commented out or not found
grep -q "upload_max_filesize = 100G" $PHP_INI || echo "upload_max_filesize = 100G" >> $PHP_INI
grep -q "post_max_size = 100G" $PHP_INI || echo "post_max_size = 100G" >> $PHP_INI
grep -q "memory_limit = 1024M" $PHP_INI || echo "memory_limit = 1024M" >> $PHP_INI
grep -q "max_execution_time = 86400" $PHP_INI || echo "max_execution_time = 86400" >> $PHP_INI

echo "=== Restarting PHP 8.3 FPM ==="
systemctl restart php8.3-fpm

echo "=== Restarting Nginx ==="
systemctl restart nginx

echo "=== Performance Optimizations Done ==="
