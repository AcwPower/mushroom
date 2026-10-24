#!/usr/bin/env bash
set -euo pipefail
ip=${DEPLOY_PUBLIC_IP:?Set DEPLOY_PUBLIC_IP}
app=/opt/mushroom-king-timer
test -f "/etc/letsencrypt/live/$ip/fullchain.pem"
sed "s/__IP__/$ip/g" "$app/ops/nginx-ip-https.conf" > /etc/nginx/sites-available/mushroom-timer
nginx -t
systemctl reload nginx
cp "$app/ops/mushroom-cert-renew.service" "$app/ops/mushroom-cert-renew.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now certbot.timer 2>/dev/null || true
systemctl enable --now mushroom-cert-renew.timer
curl --noproxy '*' --connect-to "$ip:443:127.0.0.1:443" --max-time 10 --fail --silent "https://$ip/api/health"
echo
echo 'HTTPS enabled and certificate renewal timer installed.'
