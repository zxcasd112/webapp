FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8080

EXPOSE 8080

CMD ["python", "bot.py"]
