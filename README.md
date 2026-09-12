# Robinhood Agentic Trading Platform

A production-ready agentic trading platform powered by **Google Gemini 2.0** and **Robinhood's Official Model Context Protocol (MCP)** server. Features an interactive web dashboard, both **Autonomous (Hands-Free)** and **Supervised (Human-in-the-Loop)** strategy execution modes, Antigravity MCP integration, Docker containerization, and Kubernetes deployment manifests.

---

## Key Features

- **Autonomous Hands-Free Strategy Engine**:
  - Runs continuous background evaluation loops (default: every 5 minutes).
  - Evaluates whitelisted assets using Gemini 2.0 reasoning and technical indicators.
  - Automatically executes verified buy/sell orders via Robinhood MCP without waiting for manual clicks.
- **Supervised Mode with Interactive Approval Cards**:
  - Instantly switchable via the dashboard header.
  - Generates actionable trade proposal cards with complete reasoning, pricing, and "Approve / Reject" controls.
- **Strict Ring-Fencing & Risk Guardrails**:
  - **Single Order Cap**: Prevents orders over `$MAX_ORDER_USD` (default: \$100).
  - **Daily Spend Ceiling**: Hard stop on cumulative daily purchases (`$DAILY_TRADE_LIMIT_USD`, default: \$500).
  - **Asset Whitelist**: Restricts the agent to approved tickers only (e.g. `SPY, QQQ, AAPL, NVDA, TSLA`).
  - **Account Isolation**: Ring-fences the agent to standard taxable individual brokerage accounts (strictly blocks IRAs/joint accounts).
  - **Default Paper Trading (`DRY_RUN=true`)**: Simulated execution by default for safe experimentation.
- **Robinhood SSO Authentication Gate**:
  - Web dashboard and all APIs protected behind Robinhood account login (`/login`).
  - Supports Authenticator app (TOTP), SMS, and in-app push verification.
  - Secure signed JWT session cookies (`session_token`) with optional email whitelist (`AUTHORIZED_EMAILS`).
- **Real-Time Web Dashboard**:
  - Live portfolio equity, cash, buying power, and daily return.
  - Interactive holdings table with unrealized P&L.
  - Live Server-Sent Events (SSE) strategy activity feed.
  - Gemini Trading Copilot conversational chat.
- **Antigravity MCP Integration**:
  - Global (`~/.gemini/config/mcp_config.json`) and project-level (`.agents/mcp_config.json`) configurations using `mcp-remote`.
- **Docker & Kubernetes Ready**:
  - Multi-runtime Dockerfile (Python + Node.js/npx).
  - Full Kubernetes manifests for `namespace: robinhood` and Ingress with automated TLS.

---

## Directory Structure

```
robinhood/
├── pyproject.toml               # Python project dependencies
├── .env.example                 # Configuration template
├── .gitignore                   # Excludes tokens, venv, secrets
├── Dockerfile                   # Python + Node.js runtime container
├── .dockerignore                # Container exclusions
├── README.md                    # Documentation
├── run.py                       # Local app runner
├── .agents/
│   └── mcp_config.json          # Workspace Antigravity MCP configuration
├── src/
│   ├── backend/
│   │   ├── app.py               # FastAPI backend with REST & SSE streams
│   │   ├── config.py            # Pydantic Settings configuration
│   │   ├── guardrails.py        # Risk rules, whitelist, and ring-fencing
│   │   ├── strategies.py        # Trading strategy algorithms (Dip-Buyer, DCA, Rebalancer)
│   │   ├── engine.py            # Autonomous background loop & event broadcaster
│   │   ├── agent.py             # Gemini 2.0 reasoning and conversational agent
│   │   └── mcp_client.py        # Robinhood MCP client over mcp-remote stdio
│   └── frontend/
│       └── index.html           # Dark-mode reactive trading dashboard
├── k8s/
│   ├── namespace.yaml           # Dedicated 'robinhood' namespace
│   ├── configmap.yaml           # Platform configuration settings
│   ├── secret.example.yaml      # Gemini API key secret template
│   ├── pvc.yaml                 # Persistent storage for ~/.mcp-auth tokens
│   ├── deployment.yaml          # Application deployment (1 replica, probes)
│   ├── service.yaml             # ClusterIP service on port 8000
│   └── ingress.yaml             # Ingress configuration with TLS
└── tests/
    ├── test_guardrails.py       # Unit tests for risk limits and ring-fencing
    └── test_engine.py           # Unit tests for autonomous/supervised execution
```

---

## Step 1: One-Time Robinhood MCP Authentication

Robinhood's hosted server (`https://agent.robinhood.com/mcp/trading`) uses an interactive browser authentication handshake. To authorize your environment once and cache the session token in `~/.mcp-auth`:

```bash
npx -y mcp-remote https://agent.robinhood.com/mcp/trading
```

A browser window will open to Robinhood. Log in and approve the connection. Once complete, the session token is saved in `~/.mcp-auth` and will be reused automatically.

---

## Step 2: Antigravity MCP Setup

Antigravity is configured to connect to Robinhood via `mcp-remote`:

**Config File**: `~/.gemini/config/mcp_config.json` and `.agents/mcp_config.json`:
```json
{
  "mcpServers": {
    "robinhood": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://agent.robinhood.com/mcp/trading"
      ]
    }
  }
}
```

In Antigravity, Robinhood tools will automatically appear under **Left Sidebar > Skills & Customizations**.

---

## Step 3: Local Web Dashboard Setup

### 1. Create and Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure `.env`
```bash
cp .env.example .env
```
Add your `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/)).

### 3. Run the Dashboard
```bash
python run.py
```
Open **`http://localhost:8000`** in your browser to access the dashboard.

---

## Step 4: Docker Containerization

To build the Docker container:

```bash
docker build -t robinhood-agent:latest .
```

To run locally with Docker:
```bash
docker run -p 8000:8000 \
  -e GEMINI_API_KEY="your_gemini_key" \
  -v ~/.mcp-auth:/root/.mcp-auth \
  robinhood-agent:latest
```

---

## Step 5: Kubernetes Deployment

### 1. Create Namespace & Secret
```bash
kubectl apply -f k8s/namespace.yaml

# Create secret with your Gemini API key:
kubectl create secret generic robinhood-agent-secret \
  --namespace=robinhood \
  --from-literal=GEMINI_API_KEY="your_real_gemini_api_key"
```

### 2. Deploy ConfigMap, PVC, Deployment, Service, and Ingress
```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

### 3. Copy Authentication Tokens to the Cluster (One-Time)
To seed the cluster's PersistentVolume with your authenticated `~/.mcp-auth` tokens:
```bash
# Get pod name
POD_NAME=$(kubectl get pods -n robinhood -l app=robinhood-agent -o jsonpath="{.items[0].metadata.name}")

# Copy cached tokens
kubectl cp ~/.mcp-auth/. robinhood/$POD_NAME:/root/.mcp-auth/
```

### 4. Access Your Live Platform
Navigate to your configured Ingress host (e.g., **`https://trade.yourdomain.com`**).

---

## Running Automated Tests

Run the test suite:

```bash
source .venv/bin/activate
pytest -v
```
