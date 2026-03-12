FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install sentence-transformers for local embeddings
RUN pip install --no-cache-dir sentence-transformers

# Copy application code
COPY src/ ./src/
COPY config.yaml .

# Create non-root user and fix ownership
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --home /app --no-create-home appuser \
    && mkdir -p /app/data /app/.cache \
    && chown -R appuser:appuser /app

# Expose dashboard port
EXPOSE 8000

# Drop to non-root user
USER appuser

# Run the application
CMD ["python", "-m", "src.main"]
