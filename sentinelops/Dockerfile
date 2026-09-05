FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sentinelops ./sentinelops
ARG SENTINELOPS_EXTRAS=api,llm
RUN pip install --no-cache-dir ".[${SENTINELOPS_EXTRAS}]"
EXPOSE 8000
CMD ["sentinelops", "serve", "--host", "0.0.0.0", "--port", "8000"]
