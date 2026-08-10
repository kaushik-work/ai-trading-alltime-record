FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# `python docker/brain_guard.py` puts /app/docker on sys.path, NOT /app, so
# top-level packages like `nse` are invisible and every import fails. The
# services that run `uvicorn api.server:app` or `python -c` were unaffected
# because those resolve against the working directory, which is why this only
# surfaced when a script was invoked by path.
ENV PYTHONPATH=/app

# Create db directory for SQLite
RUN mkdir -p db logs journals

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI server
CMD ["python", "-m", "uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
