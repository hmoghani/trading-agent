#!/usr/bin/env bash
set -e

# Configuration
IMAGE_NAME="${DOCKER_IMAGE:-hmoghani/robinhood-agent:latest}"
NAMESPACE="${K8S_NAMESPACE:-robinhood}"
PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"

echo "=================================================="
echo "🚀 Robinhood Agentic Trading Deployment Automation"
echo "=================================================="
echo "Image:     $IMAGE_NAME"
echo "Namespace: $NAMESPACE"
echo "Platform:  $PLATFORM"
echo "=================================================="

# Parse flags
BUILD=true
APPLY=true
RESTART=true
SYNC_TOKENS=false

for arg in "$@"; do
  case $arg in
    --build-only)
      APPLY=false
      RESTART=false
      shift
      ;;
    --k8s-only)
      BUILD=false
      shift
      ;;
    --restart)
      BUILD=false
      APPLY=false
      RESTART=true
      shift
      ;;
    --sync-tokens)
      SYNC_TOKENS=true
      shift
      ;;
    --help|-h)
      echo "Usage: ./deploy.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --build-only     Build & push Docker image only (no k8s changes)"
      echo "  --k8s-only       Apply Kubernetes manifests only (no Docker build)"
      echo "  --restart        Quick rolling restart of the live Kubernetes pod"
      echo "  --sync-tokens    Copy local ~/.mcp-auth tokens into the live cluster pod"
      echo "  --help, -h       Show this help message"
      exit 0
      ;;
  esac
done

# Step 1: Build & Push Docker Image
if [ "$BUILD" = true ]; then
  echo ""
  echo "📦 Step 1: Building Docker image ($PLATFORM)..."
  docker build --platform "$PLATFORM" -t "$IMAGE_NAME" .

  echo ""
  echo "⬆️  Step 2: Pushing to Docker registry..."
  docker push "$IMAGE_NAME"
fi

# Step 2: Apply Kubernetes Manifests
if [ "$APPLY" = true ]; then
  echo ""
  echo "☸️  Step 3: Applying Kubernetes manifests..."

  kubectl apply -f k8s/namespace.yaml
  kubectl apply -f k8s/configmap.yaml
  kubectl apply -f k8s/pvc.yaml

  # Secret: Use git-ignored local override if present
  if [ -f "k8s/local/secret.yaml" ]; then
    echo "  -> Applying local secret from k8s/local/secret.yaml"
    kubectl apply -f k8s/local/secret.yaml
  elif kubectl get secret robinhood-agent-secret -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "  -> Cluster secret 'robinhood-agent-secret' already exists, preserving it."
  else
    echo "  ⚠️  No secret found. Create k8s/local/secret.yaml or apply k8s/secret.example.yaml"
  fi

  kubectl apply -f k8s/deployment.yaml
  kubectl apply -f k8s/service.yaml

  # Ingress: Use git-ignored local override if present
  if [ -f "k8s/local/ingress.yaml" ]; then
    echo "  -> Applying local ingress from k8s/local/ingress.yaml"
    kubectl apply -f k8s/local/ingress.yaml
  else
    echo "  -> Applying standard ingress from k8s/ingress.yaml"
    kubectl apply -f k8s/ingress.yaml
  fi
fi

# Step 3: Rolling Restart
if [ "$RESTART" = true ]; then
  echo ""
  echo "🔄 Step 4: Rolling restart of deployment/robinhood-agent..."
  kubectl rollout restart deployment/robinhood-agent -n "$NAMESPACE"
  echo "⏳ Waiting for rollout to complete..."
  kubectl rollout status deployment/robinhood-agent -n "$NAMESPACE" --timeout=120s
fi

# Step 4: Sync Tokens if requested
if [ "$SYNC_TOKENS" = true ]; then
  echo ""
  echo "🔑 Step 5: Syncing ~/.mcp-auth tokens to cluster pod..."
  POD_NAME=$(kubectl get pods -n "$NAMESPACE" -l app=robinhood-agent -o jsonpath="{.items[0].metadata.name}")
  if [ -n "$POD_NAME" ]; then
    kubectl cp ~/.mcp-auth/. "$NAMESPACE/$POD_NAME:/root/.mcp-auth/"
    echo "✅ Tokens synced to pod: $POD_NAME"
  else
    echo "⚠️  No running pod found in namespace $NAMESPACE"
  fi
fi

echo ""
echo "=================================================="
echo "✅ Deployment complete!"
if [ -f "k8s/local/ingress.yaml" ]; then
  HOST=$(grep "host:" k8s/local/ingress.yaml | head -n 1 | awk '{print $2}')
  echo "🌐 Dashboard URL: https://$HOST"
fi
echo "=================================================="
