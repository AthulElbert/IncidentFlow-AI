FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py
COPY app_config.json /app/app_config.json

EXPOSE 8010
CMD ["python", "app.py"]
