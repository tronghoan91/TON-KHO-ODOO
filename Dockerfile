FROM python:3.11-slim

WORKDIR /app

# Cập nhật pip và buộc cài đúng bản mới nhất của aiogram
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --force-reinstall -r requirements.txt

COPY . .

CMD ["python", "main.py"]
