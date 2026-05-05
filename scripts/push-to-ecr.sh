#!/usr/bin/env bash
#
# push-to-ecr.sh — build all 6 service images and push them to ECR.
#
# Usage:
#   ./scripts/push-to-ecr.sh              # tags with git short-sha + :latest
#   ./scripts/push-to-ecr.sh v1.0.0       # tags with v1.0.0      + :latest
#
# Prereqs:
#   - aws CLI configured with push perms (ecr:PutImage, ecr:InitiateLayerUpload, ...)
#   - docker daemon running
#   - run from the repo root
#
# Notes:
#   --platform linux/amd64 is mandatory if you build on Apple Silicon — EC2 is x86_64
#   and will refuse "exec format error" otherwise.
#
set -euo pipefail

AWS_REGION=${AWS_REGION:-us-east-1}
AWS_ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY=$AWS_ACCT.dkr.ecr.$AWS_REGION.amazonaws.com

# Tag: arg1, or git short-sha, or "dev"
TAG=${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}

echo "──────────────────────────────────────────────────────────"
echo "Pushing to:    $ECR_REGISTRY"
echo "Tag:           $TAG  (also tagging :latest)"
echo "──────────────────────────────────────────────────────────"

# ── docker login (token expires 12h) ──────────────────────────────────────────
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

# ── service_name → build context ──────────────────────────────────────────────
SERVICES=(
  "api-service:./api-service"
  "core-service:./demo-services/core-service"
  "fallback-service:./fallback-service"
  "recovery-agent:./recovery-agent"
  "payment-service:./demo-services/payment-service"
  "movie-service:./demo-services/movie-service"
)

for entry in "${SERVICES[@]}"; do
  name=${entry%%:*}
  ctx=${entry#*:}
  img="$ECR_REGISTRY/self-healing/$name:$TAG"
  latest="$ECR_REGISTRY/self-healing/$name:latest"

  echo ""
  echo "── building $name ($ctx)"
  docker build --platform linux/amd64 -t "$img" -t "$latest" "$ctx"

  echo "── pushing $img"
  docker push "$img"
  docker push "$latest"
done

echo ""
echo "✅ All 6 images pushed with tag: $TAG"
echo ""
echo "Next: on EC2 run  ./scripts/deploy-from-ecr.sh $TAG"
