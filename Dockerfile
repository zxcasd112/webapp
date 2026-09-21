FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8080
ENV PYTHONUNBUFFERED=1
# На бесплатном Render диск эфемерный. Для постоянного хранения подключите
# Render Disk (платно) и укажите DATA_DIR=/var/data
ENV DATA_DIR=/app/data

EXPOSE 8080

CMD ["python", "bot.py"]
