#!/usr/bin/env bash
set -euo pipefail
umask 027

stage=/tmp/mushroom-king-timer-stage
app=/opt/mushroom-king-timer
env_file=/etc/mushroom-king-timer.env
ip=${DEPLOY_PUBLIC_IP:?Set DEPLOY_PUBLIC_IP to the CVM public address}
test -f "$stage/server.py"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip nodejs nginx mysql-server mysql-client
systemctl enable --now mysql nginx

id mushroom >/dev/null 2>&1 || useradd --system --home "$app" --shell /usr/sbin/nologin mushroom
install -d -m 755 -o root -g mushroom "$app"
cp -a "$stage/." "$app/"
chown -R root:mushroom "$app"
chmod -R g+rX "$app"
chmod o+x "$app"

if ! test -f "$env_file"; then
    db_pass=$(openssl rand -hex 24)
    admin_pass=$(openssl rand -hex 24)
    cat > "$env_file" <<EOF
APP_ENV=production
APP_BASE_URL=https://$ip
ADMIN_PASSWORD=$admin_pass
HOST=127.0.0.1
PORT=8000
STATIC_DIR=$app/dist
DB_ENGINE=mysql
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=mushroom_timer
DB_USER=mushroom_app
DB_PASSWORD=$db_pass
SMS_MODE=disabled
CLAIM_TIMEOUT_MINUTES=90
CLAIM_PENALTY_MINUTES=5
EOF
    chmod 600 "$env_file"
    mysql <<EOF
CREATE DATABASE IF NOT EXISTS mushroom_timer CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'mushroom_app'@'127.0.0.1' IDENTIFIED BY '$db_pass';
GRANT ALL PRIVILEGES ON mushroom_timer.* TO 'mushroom_app'@'127.0.0.1';
FLUSH PRIVILEGES;
EOF
fi

cp "$app/ops/mysql-local.cnf" /etc/mysql/mysql.conf.d/99-mushroom-local.cnf
chmod 644 /etc/mysql/mysql.conf.d/99-mushroom-local.cnf
systemctl restart mysql
python3 -m venv "$app/.venv"
"$app/.venv/bin/pip" install --disable-pip-version-check -r "$app/requirements.txt"
cd "$app"
node scripts/build-frontend.mjs
chown -R root:mushroom "$app/.venv" "$app/dist"
chmod -R g+rX "$app/.venv"
chmod -R a+rX "$app/dist"

cp "$app/ops/mushroom-timer.service" /etc/systemd/system/
install -d -m 700 -o mushroom -g mushroom /var/backups/mushroom-king-timer
cp "$app/ops/mushroom-backup.service" "$app/ops/mushroom-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mushroom-timer
for i in $(seq 1 20); do
    if curl --fail --silent http://127.0.0.1:8000/api/health; then break; fi
    sleep 1
done
curl --fail --silent http://127.0.0.1:8000/api/health
systemctl enable --now mushroom-backup.timer
systemctl start mushroom-backup.service

if test -f "/etc/letsencrypt/live/$ip/fullchain.pem"; then
    DEPLOY_PUBLIC_IP="$ip" bash "$app/ops/enable-ip-https.sh"
else
    sed "s/timer.example.com/$ip/g" "$app/ops/nginx-site.conf" > /etc/nginx/sites-available/mushroom-timer
    ln -sfn /etc/nginx/sites-available/mushroom-timer /etc/nginx/sites-enabled/mushroom-timer
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl reload nginx
fi
echo 'Application staged, service running, MySQL and backup configured.'
