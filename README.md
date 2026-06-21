# BOL Pickup Time Tahrirlovchi — Telegram Bot

## Bot nima qiladi

BOL (Bill of Lading) hujjatidagi (PDF yoki rasm) sana/vaqt yozuvini topadi,
foydalanuvchidan ELD logbook asosidagi haqiqiy vaqtni so'raydi, va shu
vaqtni hujjatdagi ASL FORMATGA mosligicha qayta yozadi — jumladan
**qo'lyozma** (handwritten) yozuvlarni ham aniqlab, ularni o'chirib,
o'rniga toza, komputer shriftidagi yangi matn yozadi.

**Muhim:** bu vosita faqat haqiqiy ELD GPS ma'lumotiga mos kelmaydigan,
qo'lda kiritishda xato ketgan yozuvlarni to'g'irlash uchun mo'ljallangan.
Hujjatga kiritiladigan vaqt har doim haqiqiy ELD yozuviga mos bo'lishi
shart — bu javobgarlik foydalanuvchida.

## Uchta aniqlash rejimi

1. **Matnli PDF** — PDF ichida haqiqiy matn qatlami bo'lsa, to'g'ridan-to'g'ri
   PyMuPDF orqali qidiriladi (eng tez va aniq).
2. **OCR (Tesseract)** — skan/rasm bo'lsa va matn **bosma** (mashinada
   chop etilgan) bo'lsa, Tesseract orqali tez aniqlanadi.
3. **Vision (Claude API)** — agar Tesseract hech narsa topa olmasa
   (odatda **qo'lyozma** yozuvlar uchun shunday bo'ladi — masalan
   "Time In: 1345" qo'lda yozilgan raqamlar), bot Claude vision orqali
   rasmni tahlil qiladi. Bu qo'lyozmani ham yaxshi taniydi.

Vision rejimida bot topgan barcha nomzodlarni rasmda **raqamlangan qizil
doiralar** bilan belgilab yuboradi — siz mos raqamni tanlaysiz.

## Time In / Time Out avtomatik sinxronizatsiya

Agar tanlangan maydon "chiqish/pickup" turkumiga oid bo'lsa (Time Out,
Departure va h.k.) va hujjatda mos "kirish" maydoni ham topilgan bo'lsa
(Time In, Arrival), bot ikkalasi orasidagi asl vaqt farqini (masalan
1 soat 10 daqiqa) hisoblab, "✅ Tasdiqlash + Time In'ni ham mos sur"
tugmasini taklif qiladi — shunda ikkala maydon ham mantiqan izchil
qoladi.

## O'rnatish

```bash
cd bol_bot
pip install -r requirements.txt --break-system-packages
```

Tesseract OCR ham tizimda o'rnatilgan bo'lishi kerak:

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr

# macOS
brew install tesseract
```

## Kalitlar (token va API key)

1. **Telegram bot tokeni**: [@BotFather](https://t.me/BotFather) orqali
   `/newbot` buyrug'i bilan oling.
2. **Anthropic API key**: [console.anthropic.com](https://console.anthropic.com)
   dan oling — bu qo'lyozma yozuvlarni aniqlash (vision rejimi) uchun kerak.
   API chaqiruvlari pullik (har bir rasm tahlili uchun kichik to'lov).

```bash
export BOL_BOT_TOKEN="sizning_telegram_tokeningiz"
export ANTHROPIC_API_KEY="sizning_anthropic_api_keyingiz"
python3 bot.py
```

## Fayllar tuzilishi

- `bot.py` — Telegram bot logikasi (suhbat oqimi, fayl qabul qilish/yuborish)
- `datetime_utils.py` — matnli sana/vaqt formatlarini aniqlash va qayta generatsiya qilish
- `pdf_engine.py` — PDF/rasm bilan ishlash: matn qidirish, OCR, redaction, overlay chizish
- `vision_engine.py` — Claude API orqali rasm tahlili (qo'lyozma aniqlash)
- `requirements.txt` — kerakli Python kutubxonalari

## Qo'llab-quvvatlanadigan vaqt formatlari (datetime_utils.py)

- `MM/DD/YYYY HH:MM:SS AM/PM` va sekundsiz, 2/4-xonali yil variantlari
- `YYYY-MM-DD HH:MM:SS` (ISO)
- `Month DD, YYYY HH:MM:SS AM/PM` (masalan "July 13, 2026 1:34 PM")
- `DD-Mon-YY HH:MM` (masalan "20-Jun-26 23:15" — Amazon uslubi)
- Ajratuvchisiz 4-xonali `HHMM` 24-soat format, faqat "Time" so'zi
  yonida (masalan "Time In: 1345" — Walmart/Rockline uslubi)
- Sana va vaqt alohida joylarda bo'lgan holatlar

Yangi format uchun `datetime_utils.py` ichidagi `_PATTERNS` ro'yxatiga
yangi regex qo'shish orqali kengaytirish mumkin.

## Cheklovlar

- Vision (Claude) chaqiruvi internet va API kreditiga bog'liq;
  internet uzilsa yoki kredit tugasa, bot xato xabarini ko'rsatadi
- Bir vaqtning o'zida faqat bitta sahifa (PDF'ning birinchi sahifasi)
  tahlil qilinadi — ko'p sahifali hujjatlar uchun kengaytirish kerak
  bo'lishi mumkin
- Juda past sifatli yoki qiyshiq skanlarda OCR/vision aniqligi pasayishi
  mumkin — shuning uchun bot har doim "Eski qiymat / Yangi qiymat"
  ko'rsatib tasdiqlashni so'raydi
