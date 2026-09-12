# Multi-stage / combined runtime with Python and Node.js (for npx mcp-remote)
FROM python:3.12-slim

# Install system utilities and Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pre-cache mcp-remote package
RUN npx -y mcp-remote --help || true

# Install Python project dependencies
COPY pyproject.toml README.md /app/
RUN pip install --no-cache-dir -e .

# Copy application source code
COPY src /app/src
COPY run.py /app/

# Create auth token directory
RUN mkdir -p /root/.mcp-auth

# Expose web dashboard port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["python", "run.py"]
