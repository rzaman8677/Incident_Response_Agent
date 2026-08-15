FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sentinelops ./sentinelops
COPY web ./web
RUN pip install --no-cache-dir '.[api,llm]'
EXPOSE 8000
CMD ["sentinelops", "serve", "--host", "0.0.0.0", "--port", "8000"]
