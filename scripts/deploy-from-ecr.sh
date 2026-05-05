#!/usr/bin/env bash
#
# deploy-from-ecr.sh — pull latest images from ECR and (re)start the stack.
# Runs on the EC2 host. Wired into systemd so it runs on every boot.
#
# Usage:
#   ./scripts/deploy-from-ecr.sh              # pulls :latest
#   ./scripts/deploy-from-ecr.sh v1.0.0       # pulls a specific tag
#
# Prereqs on EC2:
#   - IAM role attached with AmazonEC2ContainerRegistryReadOnly
#   - docker + docker compose v2 installed
#   - this repo cloned at the path below (adjust if different)
#
set -euo pipefail

AWS_REGION=${AWS_REGION:-us-east-1}
AWS_ACCT=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY=$AWS_ACCT.dkr.ecr.$AWS_REGION.amazonaws.com
export IMAGE_TAG=${1:-latest}

REPO_DIR=${REPO_DIR:-/home/ubuntu/self-healing-system}

echo "──────────────────────────────────────────────────────────"
echo "Pulling from:  $ECR_REGISTRY"
echo "Tag:           $IMAGE_TAG"
echo "Repo dir:      $REPO_DIR"
echo "──────────────────────────────────────────────────────────"

# ── docker login (token TTL 12h — re-login each deploy) ──────────────────────
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

cd "$REPO_DIR"

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ecr.yml"

# ── pull, then up ─────────────────────────────────────────────────────────────
docker compose $COMPOSE_FILES pull
docker compose $COMPOSE_FILES up -d --remove-orphans

# ── housekeeping ──────────────────────────────────────────────────────────────
docker image prune -f >/dev/null

echo ""
echo "✅ Stack running with tag: $IMAGE_TAG"
docker compose $COMPOSE_FILES ps
