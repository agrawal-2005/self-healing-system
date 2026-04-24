FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

EXPOSE 8000

# Run via module path so Python can resolve "app.xxx" imports correctly
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
