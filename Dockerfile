FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg aria2 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot-7.py cookies.txt ./
CMD ["python", "bot-7.py"]
