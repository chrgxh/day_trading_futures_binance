FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.yaml ./
COPY core/ core/
COPY utils/ utils/

CMD ["python", "bot.py"]
