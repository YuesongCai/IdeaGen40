#!/bin/bash
# cloud-init bootstrap for the production instance.
#
# No secrets: this text is stored by the cloud API and readable from the
# console, so it only installs software and clones public code. runtime.env is
# delivered afterwards over an encrypted channel.
#
# The health page on :80 exists to answer one question the operator machine
# cannot otherwise settle: whether the instance is reachable at all. Outbound
# TCP 22 is filtered on this network and ECS Cloud Assistant fails to install
# on the current image, so "no SSH" and "no route" look identical from here.
# A static page the security group already permits tells them apart.
set -x
exec > /var/log/ideagen-bootstrap.log 2>&1
export DEBIAN_FRONTEND=noninteractive

for i in 1 2 3; do apt-get update -qq && break || sleep 10; done
apt-get install -y -qq docker.io docker-compose-v2 git curl ca-certificates

systemctl enable --now docker

mkdir -p /opt/ideagen/app /opt/ideagen/config /opt/ideagen/oauth /opt/ideagen/health
chmod 700 /opt/ideagen/config /opt/ideagen/oauth

git clone --depth 50 https://github.com/YuesongCai/IdeaGen40.git /opt/ideagen/app \
  || git -C /opt/ideagen/app pull --ff-only

{
  echo "ideagen instance up"
  date -u +%FT%TZ
  git -C /opt/ideagen/app rev-parse --short HEAD 2>/dev/null || echo "no-clone"
} > /opt/ideagen/health/index.html

# Reachability probe only — no application data, no credentials. It is replaced
# by the real proxy when the stack comes up.
docker run -d --name ideagen-health --restart always -p 80:80 \
  -v /opt/ideagen/health:/usr/share/nginx/html:ro nginx:alpine

date -u +%FT%TZ > /opt/ideagen/BOOTSTRAP_DONE
echo "bootstrap finished"
