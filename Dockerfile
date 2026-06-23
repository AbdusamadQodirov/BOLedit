FROM python:3.11-slim

# tesseract-ocr - matn/raqamlarni rasm/skan PDF'dan o'qish uchun zarur
# tizim darajasidagi dastur (pytesseract buni chaqiradi, lekin o'zi
# o'rnatmaydi - shuning uchun bu yerda alohida o'rnatamiz)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
