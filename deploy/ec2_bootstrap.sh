#!/usr/bin/env bash
# EC2 user-data script: runs once as root on first boot (Ubuntu 22.04).
# Installs the app, sets up systemd + nginx, and leaves /opt/app/.env empty
# for the operator to fill in over SSH (secrets are never passed through
# user-data, since user-data is visible to anyone with EC2 describe-instance
# permissions on the account).
set -euo pipefail

REPO_URL="https://github.com/RCNR/ASD_communication_practice_system.git"
APP_DIR="/opt/app"

apt-get update -y
apt-get install -y python3-venv python3-pip nginx git

git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

touch "$APP_DIR/.env"
cp deploy/asd-app.service /etc/systemd/system/asd-app.service
cp deploy/nginx.conf /etc/nginx/sites-available/asd-app
ln -sf /etc/nginx/sites-available/asd-app /etc/nginx/sites-enabled/asd-app
rm -f /etc/nginx/sites-enabled/default

chown -R ubuntu:ubuntu "$APP_DIR"

# Lets the GitHub Actions deploy step (which SSHes in as ubuntu, no TTY for
# a sudo password prompt) restart the service after each redeploy.
echo "ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart asd-app, /bin/systemctl start asd-app, /bin/systemctl stop asd-app" \
    > /etc/sudoers.d/asd-app-deploy
chmod 440 /etc/sudoers.d/asd-app-deploy

systemctl daemon-reload
systemctl enable asd-app
systemctl restart nginx

# asd-app is intentionally NOT started here - .env is still empty at this
# point. SSH in, fill in /opt/app/.env, then:
#   sudo systemctl start asd-app
