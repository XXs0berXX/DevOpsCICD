#!/bin/bash
# Runs ON the EC2 instance (sent by the pipeline through SSM) after the latest build zip
# has been downloaded from S3 and unpacked next to this script.
set -euo pipefail
cd "$(dirname "$0")/.."

cloud-init status --wait > /dev/null || true   # first boot: wait until Docker is installed

echo "Deploying NestMarket $(cat api/VERSION)"
cp /opt/nestmarket/.env .env                   # instance settings (tables, buckets, URLs)
docker compose up -d --build --remove-orphans
docker image prune -f > /dev/null
docker compose ps
