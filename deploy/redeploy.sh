#!/usr/bin/env bash
# Runs on the EC2 instance. Triggered over SSH by the GitHub Actions deploy
# workflow on every push to main. Never touches app.db or .env (both are
# untracked, so git leaves them alone).
set -euo pipefail

cd /opt/app
git fetch origin main
git reset --hard origin/main

source venv/bin/activate
pip install -q -r requirements.txt

sudo systemctl restart asd-app
