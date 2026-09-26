FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
COPY pyproject.toml uv.lock ./
COPY hermes_trading ./hermes_trading
COPY growth.json ./growth.json
RUN uv sync --frozen
ENV HERMES_TRADING_MODE=paper
ENV HERMES_GROWTH_STATE=/app/state/growth/account.json
CMD ["uv", "run", "python", "-m", "hermes_trading.growth_run", "paper"]
