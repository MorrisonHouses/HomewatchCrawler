FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY crawler.py worker.py ./

CMD ["python", "worker.py"]
