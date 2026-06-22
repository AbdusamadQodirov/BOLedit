# BOL Pickup Time Tahrirlovchi — Telegram Bot

## Bot nima qiladi

BOL (Bill of Lading) hujjatidagi (PDF yoki rasm) pickup'ga oid sana/vaqt
yozuvlarini topadi, time zone konvertatsiyasini qo'llab, foydalanuvchidan
ELD logbook asosidagi haqiqiy kun/vaqtni so'raydi, va shu vaqtni hujjatdagi
ASL FORMATGA mosligicha qayta yozadi — jumladan **qo'lyozma** yozuvlarni
ham aniqlab, ularni o'chirib, o'rniga toza, komputer shriftidagi yangi
matn yozadi.

**Muhim:** bu vosita faqat haqiqiy ELD GPS ma'lumotiga mos kelmaydigan,
qo'lda kiritishda xato ketgan yozuvlarni to'g'irlash uchun mo'ljallangan.
Hujjatga kiritiladigan vaqt har doim haqiqiy ELD yozuviga mos bo'lishi
shart — bu javobgarlik foydalanuvchida.

## To'liq foydalanuvchi oqimi

1. **/start** (yoki "▶️ Start" tugmasi) — bot ishga tushadi
2. BOL faylini yuborasiz (PDF yoki rasm)
3. Bot fonni avtomatik kesadi, aylanishni to'g'irlaydi, sana/vaqt
   yozuvlarini topadi (matn/OCR/vision orqali)
4. Qaysi yozuvni tahrirlash kerakligini tanlaysiz
5. **Company time zone** tanlanadi: EDT / CDT / MDT / PDT (4 tugma)
6. Bot hujjat matnidagi **ORIGIN/Ship From** manzilidan pickup joyining
   time zone'ini avtomatik aniqlaydi
7. **Oy** tanlanadi (12 tugma)
8. **Yil** tanlanadi (3 tugma: o'tgan/joriy/keyingi yil)
9. **Kun va vaqt** qo'lda kiritiladi (masalan "13 1:34:45 PM") — bu
   company time zone bo'yicha deb hisoblanadi
10. Bot company tz'dan pickup tz'ga avtomatik konvertatsiya qiladi
    (DST/yozgi-qishki vaqt hisobga olingan holda)
11. Eski/yangi qiymat ko'rsatilib tasdiqlash so'raladi
12. Tayyor hujjat (PDF) qaytariladi
13. **/stop** (yoki "⏹ Stop" tugmasi) — bot to'xtatiladi, joriy jarayon
    bekor qilinadi; qaytadan ishlatish uchun /start kerak

## Asosiy imkoniyatlar

### Start/Stop boshqaruvi
Ekranning pastida doimiy "▶️ Start" / "⏹ Stop" tugmalari ko'rinadi.
`/stop` bosilganda barcha joriy jarayon bekor qilinadi va bot hech qanday
faylga javob bermaydi, toki "▶️ Start" yoki `/start` bosilmaguncha.

### Time zone konvertatsiyasi
Foydalanuvchi tanlagan company tz (EDT/CDT/MDT/PDT) va hujjatdan avtomatik
aniqlangan pickup tz orasidagi farq hisobga olinib, kiritilgan vaqt to'g'ri
zonaga o'giriladi. Agar pickup tz avtomatik aniqlanmasa, vaqt konvertatsiyasiz,
kiritilgani kabi ishlatiladi (bu holat foydalanuvchiga xabar qilinadi).

Pickup manzili sifatida faqat **ORIGIN / Ship From / Shipper** yorlig'i
ostidagi manzil olinadi — DESTINATION / Ship To / Consignee qismidagi
manzillar e'tiborga olinmaydi (`timezone_utils.py` da `_ORIGIN_LABELS` /
`_DESTINATION_LABELS` ro'yxatlari orqali sozlanadi).

### Avtomatik fon tozalash va aylanish tuzatish
Foto orqali yuklangan hujjatlarda stol/qora fon avtomatik kesib
tashlanadi, matn yo'nalishi (90°/180°/270°) avtomatik to'g'irlanadi.

### Uchta aniqlash rejimi
1. **Matnli PDF** — to'g'ridan-to'g'ri PyMuPDF orqali qidiriladi.
2. **OCR (Tesseract)** — skan/rasm, **bosma** matn uchun.
3. **Vision (Claude API)** — Tesseract topa olmasa (odatda **qo'lyozma**
   uchun), Claude vision orqali tahlil qilinadi, natijalar rasmda
   raqamlangan qizil doiralar bilan ko'rsatiladi.

### Vaqt ORALIG'I qo'llab-quvvatlash
"06:00-10:00" kabi vaqt oralig'i bitta nomzod sifatida tanilib, yangi
boshlanish vaqti kiritilganda asl davomiylik saqlangan holda tugash
vaqti ham qayta hisoblanadi.

### Time In / Time Out va Pickup-guruh sinxronizatsiyasi
- Time Out tanlansa va hujjatda Time In ham bo'lsa, ikkalasi orasidagi
  asl farq saqlangan holda birga yangilash taklif qilinadi.
- SHIP DATE / ORIGIN / Shipper Signature kabi bir nechta joyda
  takrorlangan pickup-bog'liq maydonlar ham xuddi shunday birga
  sinxronlanadi.

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
   dan oling — qo'lyozma yozuvlarni aniqlash (vision rejimi) uchun kerak.

```bash
export BOL_BOT_TOKEN="sizning_telegram_tokeningiz"
export ANTHROPIC_API_KEY="sizning_anthropic_api_keyingiz"
python3 bot.py
```

## Fayllar tuzilishi

- `bot.py` — Telegram bot logikasi: suhbat oqimi (fayl → nomzod tanlash →
  timezone → oy → yil → kun/vaqt → tasdiqlash), start/stop boshqaruvi,
  Time In/Out va pickup-guruh sinxronizatsiyasi
- `datetime_utils.py` — matnli sana/vaqt formatlarini aniqlash va qayta
  generatsiya qilish (jumladan vaqt oralig'i)
- `pdf_engine.py` — PDF/rasm bilan ishlash: matn qidirish, OCR, redaction,
  raqamlangan overlay chizish
- `vision_engine.py` — Claude API orqali rasm tahlili (qo'lyozma aniqlash)
- `document_scanner.py` — avtomatik fon-kesish (auto-crop) va aylanish-tuzatish
- `timezone_utils.py` — shtat/shahar nomidan time zone aniqlash, company-tz
  dan pickup-tz ga vaqt konvertatsiyasi
- `requirements.txt` — kerakli Python kutubxonalari

## Qo'llab-quvvatlanadigan vaqt formatlari (datetime_utils.py)

- `MM/DD/YYYY HH:MM:SS AM/PM` va sekundsiz, 2/4-xonali yil variantlari
- `YYYY-MM-DD HH:MM:SS` (ISO)
- `Month DD, YYYY HH:MM:SS AM/PM`
- `DD-Mon-YY HH:MM` (Amazon uslubi)
- `MM/DD/YYYY HH:MM-HH:MM` vaqt ORALIG'I (Armstrong uslubi)
- Ajratuvchisiz 4-xonali `HHMM` 24-soat format, faqat "Time" so'zi yonida
- Sana va vaqt alohida joylarda bo'lgan holatlar

## Sozlash

- **Time zone tanlovlari**: `bot.py` dagi `_TIMEZONE_OPTIONS` ro'yxati
- **Shtat → time zone xaritasi**: `timezone_utils.py` dagi `_STATE_TO_IANA`
- **Pickup/Destination yorliqlari**: `timezone_utils.py` dagi
  `_ORIGIN_LABELS` / `_DESTINATION_LABELS`
- **Pickup guruhi kalit so'zlari**: `bot.py` dagi `_PICKUP_GROUP_KEYWORDS`

## Cheklovlar

- Vision (Claude) chaqiruvi internet va API kreditiga bog'liq
- Bir vaqtning o'zida faqat bitta sahifa tahlil qilinadi
- Pickup tz avtomatik aniqlanishi hujjatda ORIGIN/Ship From manzili aniq
  ko'rsatilgan bo'lishiga bog'liq; topilmasa, konvertatsiya qilinmaydi
- Auto-crop/auto-rotate va OCR/vision aniqligi hujjat sifatiga bog'liq —
  shuning uchun bot har doim "Eski qiymat / Yangi qiymat" va konvertatsiya
  tafsilotlarini ko'rsatib tasdiqlashni so'raydi
