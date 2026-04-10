FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libnss3 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libgtk-3-0 \
      libgbm1 \
      libasound2 \
      libx11-xcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxrandr2 \
      libxshmfence1 \
      libxkbcommon0 \
      libxext6 \
      libxi6 \
      libxtst6 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libglib2.0-0 \
      libnspr4 \
      libpango-1.0-0 \
      libpangocairo-1.0-0 \
      libcairo2 \
      libatspi2.0-0 \
      fonts-liberation \
      fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r /app/requirements.txt pytest \
    && python -m playwright install chromium

COPY . /app

EXPOSE 5000

CMD ["python", "main.py"]
