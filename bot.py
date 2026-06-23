"""
BOL (Bill of Lading) Pickup Time Tahrirlovchi Telegram Bot
=============================================================

OQIM:
1. /start -> foydalanuvchi BOL fayl yuboradi (PDF yoki rasm: jpg/png)
2. Agar rasm bo'lsa - avval avtomatik:
   a) ortiqcha fon/stol kesib tashlanadi (auto-crop, perspektiv tuzatish)
   b) matn yo'nalishi to'g'irlanadi (auto-rotate, agar kerak bo'lsa)
3. Bot hujjat turini aniqlaydi:
   a) Matn qatlami bor PDF -> to'g'ridan-to'g'ri matn orqali qidiriladi
   b) Skan/rasm -> avval Tesseract OCR bilan tez qidiriladi (bosma matn
      uchun yaxshi ishlaydi)
   c) OCR yetarlicha topa olmasa (masalan QO'LYOZMA yozuvlar) -> Claude
      vision (Anthropic API) orqali rasm tahlil qilinadi, bu qo'lyozmani
      ham yaxshi taniydi
4. Topilgan barcha nomzodlar RAQAMLANGAN qizil ramka bilan rasmda
   ko'rsatiladi (vision/OCR yo'li uchun) yoki tugmalar ro'yxati
   sifatida (matnli PDF yo'li uchun)
5. Foydalanuvchi qaysi yozuvni tahrirlash kerakligini tanlaydi
6. Bot ELD asosidagi yangi (to'g'ri) vaqtni so'raydi
7. Yangi vaqt eski yozuv FORMATIGA moslab generatsiya qilinadi:
   - 24-soat/AM-PM, sekund bor/yo'q, sana uslubi - barchasi saqlanadi
   - VAQT ORALIG'I (masalan "06:00-10:00") bo'lsa, asl davomiylik
     saqlanib, yangi boshlanish vaqtiga moslab tugash vaqti hisoblanadi
   - QO'LYOZMA yozuvlar uchun: eski yozuv butunlay o'chiriladi va
     o'rniga TOZA, komputer shriftidagi yangi matn yoziladi
8. MAXSUS LOGIKA #1 (Time In/Time Out): agar tanlangan maydon "Time Out"
   (yoki shunga o'xshash pickup-chiqish vaqti) bo'lsa va hujjatda
   "Time In" ham topilgan bo'lsa, Time In ham xuddi shu farq (delta)
   bilan avtomatik suriladi
9. MAXSUS LOGIKA #2 (Pickup guruhi): agar tanlangan maydon SHIP DATE,
   ORIGIN yoki Shipper Signature kabi "pickup guruhi"ga oid bo'lsa,
   hujjatdagi BOSHQA shu guruhga oid barcha maydonlar ham bitta umumiy
   pickup voqeasiga mos ravishda (asl sana/vaqt farqlari saqlangan
   holda) birgalikda yangilanishi taklif qilinadi
10. Tayyor hujjat (PDF, faqat hujjatning o'zi - fon/qora stol yo'q)
    foydalanuvchiga qaytariladi

ESLATMA: bu bot faqat HAQIQIY ELD GPS ma'lumotiga mos kelmaydigan,
qo'lda yozishda xato ketgan yozuvlarni to'g'irlash uchun mo'ljallangan.
Foydalanuvchi kiritadigan vaqt har doim haqiqiy ELD yozuviga mos
bo'lishi kerak - bu botning o'zi buni tekshirmaydi, javobgarlik
foydalanuvchida.
"""

import logging
import os
import re
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import fitz
from PIL import Image
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

from datetime_utils import parse_user_input, format_like_original
from pdf_engine import (
    is_scanned_pdf, extract_text_candidates, extract_ocr_candidates,
    replace_text_in_pdf, replace_text_in_scanned_pdf, images_to_pdf_bytes,
    draw_numbered_overlay, replace_vision_candidate_in_image, page_to_image,
)
from document_scanner import auto_crop_document, auto_rotate_document
from vision_engine import find_vision_candidates, VisionCandidate
from timezone_utils import (
    guess_state_code_from_text, state_code_to_iana,
    convert_between_timezones, iana_to_abbr, TZ_ABBR_TO_IANA,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOL_BOT_TOKEN", "")

# Conversation holatlari
(
    WAITING_FILE, CHOOSING_FIELD, CHOOSING_TIMEZONE, CHOOSING_MONTH,
    CHOOSING_YEAR, WAITING_NEW_TIME, CONFIRMING, ASK_DELTA_CONFIRM,
) = range(8)

# Qo'llab-quvvatlanadigan company time zone'lari (foydalanuvchi tanlaydi)
_TIMEZONE_OPTIONS = ["EDT", "CDT", "MDT", "PDT"]

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Vaqt maydoni nomida shu so'zlardan biri bo'lsa, "chiqish/pickup vaqti" deb hisoblanadi
_OUT_KEYWORDS = ("time out", "pickup", "departure", "depart", "out")
_IN_KEYWORDS = ("time in", "arrival", "check in", "checkin", " in")

# "Pickup guruhi": bitta jismoniy voqeani (masalan yuk olib ketilishi) ifodalovchi,
# lekin hujjatda BIR NECHA joyda (turli yorliqlar ostida) takrorlanadigan maydonlar.
# Masalan Armstrong BOL'da: SHIP DATE, ORIGIN (vaqt oralig'i) va Shipper Signature
# sanasi - barchasi bitta pickup voqeasiga tegishli va birga o'zgarishi kerak.
_PICKUP_GROUP_KEYWORDS = ("ship date", "origin", "shipper signature", "signature", "pickup", "pu date", "pu #")


_ACTIVE_KEYBOARD = ReplyKeyboardMarkup(
    [["⏹ Stop"]], resize_keyboard=True, one_time_keyboard=False
)
_INACTIVE_KEYBOARD = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=False
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["bot_active"] = True
    await update.message.reply_text(
        "Salom! Men BOL (Bill of Lading) hujjatidagi pickup vaqtini "
        "ELD ma'lumotiga moslab to'g'irlashga yordam beraman.\n\n"
        "BOL faylini yuboring - PDF yoki rasm (foto) shaklida bo'lishi mumkin.\n\n"
        "Istalgan vaqtda pastdagi \"⏹ Stop\" tugmasi (yoki /stop) bilan "
        "botni to'xtatishingiz mumkin.",
        reply_markup=_ACTIVE_KEYBOARD,
    )
    return WAITING_FILE


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botni shu foydalanuvchi uchun to'xtatadi - joriy jarayon bekor qilinadi,
    qaytadan ishlatish uchun /start bosish kerak bo'ladi."""
    context.user_data.clear()
    context.user_data["bot_active"] = False
    await update.message.reply_text(
        "Bot to'xtatildi. Qaytadan ishlatish uchun pastdagi \"▶️ Start\" "
        "tugmasi (yoki /start) ni bosing.",
        reply_markup=_INACTIVE_KEYBOARD,
    )
    return ConversationHandler.END


def _is_bot_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("bot_active", True)


def _pil_from_bytes(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGB")


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDF yoki rasm (jpg/png) faylini qabul qiladi."""
    if not _is_bot_active(context):
        return ConversationHandler.END

    document = update.message.document
    photo = update.message.photo

    file_bytes: Optional[bytes] = None
    is_pdf = False

    if document:
        name = (document.file_name or "").lower()
        tg_file = await document.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
        is_pdf = name.endswith(".pdf")
        if not is_pdf and not any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
            await update.message.reply_text("Iltimos, PDF yoki rasm (jpg/png) fayl yuboring.")
            return WAITING_FILE
    elif photo:
        tg_file = await photo[-1].get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
        is_pdf = False
    else:
        await update.message.reply_text("Iltimos, PDF yoki rasm fayl yuboring.")
        return WAITING_FILE

    await update.message.reply_text("Faylni tahlil qilyapman, biroz kuting...")

    if is_pdf:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        scanned = is_scanned_pdf(doc)
    else:
        # Rasm bo'lsa - avval ortiqcha fon/stolni avtomatik kesib (auto-crop),
        # so'ng matn yo'nalishini to'g'irlab (auto-rotate), bitta sahifali
        # "PDF" yasaymiz
        pix_img = _pil_from_bytes(file_bytes)
        pix_img = auto_crop_document(pix_img)
        pix_img = auto_rotate_document(pix_img)
        buf = BytesIO()
        pix_img.save(buf, format="PDF")
        doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
        scanned = True

    context.user_data["is_scanned"] = scanned
    context.user_data["original_pdf_bytes"] = doc.tobytes() if not scanned else None

    if not scanned:
        # --- MATN QATLAMI BOR PDF ---
        candidates = extract_text_candidates(doc)
        context.user_data["mode"] = "text"
        context.user_data["candidates"] = candidates
        context.user_data["full_doc_text"] = doc[0].get_text("text")

        if not candidates:
            await update.message.reply_text(
                "Bu hujjatdan sana/vaqt yozuvlarini topa olmadim. Boshqa fayl yuboring."
            )
            return WAITING_FILE

        buttons = []
        for idx, cand in enumerate(candidates):
            label = f"{idx+1}. {cand.tm.raw_text}  ({cand.context[-30:]})"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{idx}")])
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Quyidagi sana/vaqt yozuvlari topildi. Qaysi birini tahrirlash kerak?",
            reply_markup=markup,
        )
        return CHOOSING_FIELD

    # --- SKAN / RASM ---
    # Avval tez Tesseract OCR bilan urinib ko'ramiz (bosma matn uchun yaxshi)
    ocr_candidates, page_images = extract_ocr_candidates(doc)
    page_img = page_images[0] if page_images else page_to_image(doc[0])

    # Pickup-manzilni (va shtatini) aniqlash uchun to'liq matnni saqlab qo'yamiz
    try:
        import pytesseract
        context.user_data["full_doc_text"] = pytesseract.image_to_string(page_img)
    except Exception:
        context.user_data["full_doc_text"] = ""

    # Agar Tesseract yetarlicha topa olmasa (masalan qo'lyozma ko'p bo'lsa),
    # Claude vision'ga murojaat qilamiz
    use_vision = len(ocr_candidates) == 0
    vision_candidates = []

    if use_vision:
        try:
            vision_candidates = find_vision_candidates(page_img)
        except RuntimeError as e:
            await update.message.reply_text(
                f"Avtomatik aniqlashda xatolik: {e}\n"
                "ANTHROPIC_API_KEY sozlanmagan bo'lishi mumkin."
            )
            if not ocr_candidates:
                return WAITING_FILE

    if vision_candidates:
        context.user_data["mode"] = "vision"
        context.user_data["page_image"] = page_img
        context.user_data["vision_candidates"] = vision_candidates

        overlay_img = draw_numbered_overlay(page_img, vision_candidates)
        buf = BytesIO()
        overlay_img.save(buf, format="PNG")
        buf.seek(0)

        lines = []
        for idx, c in enumerate(vision_candidates, start=1):
            tag = "✍️ qo'lyozma" if c.is_handwritten else "🖨️ bosma"
            lines.append(f"{idx}. {c.raw_text}  —  {c.context}  ({tag})")
        text_list = "\n".join(lines)

        buttons = [
            [InlineKeyboardButton(str(idx), callback_data=f"vpick_{idx-1}")]
            for idx in range(1, len(vision_candidates) + 1)
        ]
        markup = InlineKeyboardMarkup(buttons)

        await update.message.reply_photo(
            photo=buf,
            caption=(
                "Rasmda topilgan sana/vaqt yozuvlari (raqamlar bo'yicha):\n\n"
                f"{text_list}\n\nQaysi raqamni tahrirlash kerak?"
            ),
            reply_markup=markup,
        )
        return CHOOSING_FIELD

    elif ocr_candidates:
        context.user_data["mode"] = "ocr"
        context.user_data["page_images"] = page_images
        context.user_data["candidates"] = ocr_candidates

        buttons = []
        for idx, cand in enumerate(ocr_candidates):
            label = f"{idx+1}. {cand.tm.raw_text}  ({cand.context[-30:]})"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{idx}")])
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Quyidagi sana/vaqt yozuvlari topildi. Qaysi birini tahrirlash kerak?",
            reply_markup=markup,
        )
        return CHOOSING_FIELD

    else:
        await update.message.reply_text(
            "Hujjatdan sana/vaqt yozuvlarini topa olmadim. Boshqa fayl yuboring."
        )
        return WAITING_FILE


async def choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    mode = context.user_data.get("mode")

    if query.data.startswith("vpick_"):
        idx = int(query.data.split("_")[1])
        context.user_data["chosen_idx"] = idx
        cand = context.user_data["vision_candidates"][idx]
        old_text = cand.raw_text
        context_label = cand.context
    else:
        idx = int(query.data.split("_")[1])
        context.user_data["chosen_idx"] = idx
        cand = context.user_data["candidates"][idx]
        old_text = cand.tm.raw_text
        context_label = cand.context

    tz_buttons = [
        [InlineKeyboardButton(tz, callback_data=f"tz_{tz}") for tz in _TIMEZONE_OPTIONS]
    ]
    markup = InlineKeyboardMarkup(tz_buttons)

    await query.message.reply_text(
        f"Tanlandi: {old_text}  ({context_label})\n\n"
        f"Avval company'ning time zone'ini tanlang:",
        reply_markup=markup,
    )
    return CHOOSING_TIMEZONE


async def choose_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tz = query.data.split("_", 1)[1]
    context.user_data["company_timezone"] = tz

    # Pickup joyining time zone'ini hujjat matnidan avtomatik aniqlashga harakat qilamiz
    full_text = context.user_data.get("full_doc_text", "") or ""
    state_code = guess_state_code_from_text(full_text)
    pickup_iana = state_code_to_iana(state_code) if state_code else None

    if pickup_iana:
        pickup_abbr = iana_to_abbr(pickup_iana)
        context.user_data["pickup_iana"] = pickup_iana
        context.user_data["pickup_abbr"] = pickup_abbr
        tz_note = (
            f"Pickup joyi hujjatdan avtomatik aniqlandi: {state_code} "
            f"({pickup_abbr})\n"
            f"Vaqt {tz} dan {pickup_abbr} ga avtomatik konvertatsiya qilinadi."
        )
    else:
        context.user_data["pickup_iana"] = None
        context.user_data["pickup_abbr"] = None
        tz_note = (
            "Pickup joyining time zone'ini hujjatdan avtomatik aniqlay olmadim - "
            "vaqt KONVERTATSIYASIZ, company tz bo'yicha kiritilgani kabi yoziladi."
        )

    month_buttons = []
    row = []
    for i, name in enumerate(_MONTH_NAMES, start=1):
        row.append(InlineKeyboardButton(name[:3], callback_data=f"month_{i}"))
        if len(row) == 4:
            month_buttons.append(row)
            row = []
    if row:
        month_buttons.append(row)

    markup = InlineKeyboardMarkup(month_buttons)
    await query.edit_message_text(
        f"Time zone: {tz}\n{tz_note}\n\nEndi oyni tanlang:",
        reply_markup=markup,
    )
    return CHOOSING_MONTH
    return CHOOSING_MONTH


async def choose_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    month_num = int(query.data.split("_", 1)[1])
    context.user_data["chosen_month"] = month_num
    month_name = _MONTH_NAMES[month_num - 1]

    current_year = datetime.now().year
    years = [current_year - 1, current_year, current_year + 1]
    year_buttons = [[InlineKeyboardButton(str(y), callback_data=f"year_{y}") for y in years]]
    markup = InlineKeyboardMarkup(year_buttons)

    await query.edit_message_text(
        f"Time zone: {context.user_data['company_timezone']}\n"
        f"Oy: {month_name}\n\nEndi yilni tanlang:",
        reply_markup=markup,
    )
    return CHOOSING_YEAR


async def choose_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    year = int(query.data.split("_", 1)[1])
    context.user_data["chosen_year"] = year
    month_name = _MONTH_NAMES[context.user_data["chosen_month"] - 1]
    tz = context.user_data["company_timezone"]

    await query.edit_message_text(
        f"Time zone: {tz}\n"
        f"Oy: {month_name}\n"
        f"Yil: {year}\n\n"
        f"Endi ELD logbook'dagi HAQIQIY kun va vaqtni qo'lda kiriting.\n"
        f"Masalan: 13 1:34:45 PM\n"
        f"yoki: 13 13:34:45\n"
        f"yoki shunchaki: 13 13:34"
    )
    return WAITING_NEW_TIME


def _guess_fallback_year(text: str) -> int:
    ym = re.search(r'(20\d{2})', text)
    if ym:
        return int(ym.group(1))
    return datetime.now().year


def _parse_day_time_input(text: str, month: int, year: int) -> Optional[datetime]:
    """
    Foydalanuvchi kiritgan "KUN VAQT" formatidagi matnni (masalan
    "13 1:34:45 PM" yoki "13 13:34:45" yoki "13 13:34") tanlangan
    oy/yil bilan birlashtirib, to'liq datetime obyektiga aylantiradi.
    """
    text = text.strip()
    m = re.match(
        r'^(?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(:(?P<second>\d{2}))?'
        r'\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?$',
        text,
    )
    if not m:
        return None

    day = int(m.group("day"))
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    second = int(m.group("second") or 0)

    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


async def receive_new_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]

    if mode == "vision":
        cand = context.user_data["vision_candidates"][idx]
        old_raw = cand.raw_text
    else:
        cand = context.user_data["candidates"][idx]
        old_raw = cand.tm.raw_text

    month = context.user_data.get("chosen_month")
    year = context.user_data.get("chosen_year")

    new_dt = _parse_day_time_input(text, month, year) if (month and year) else None
    if new_dt is None:
        await update.message.reply_text(
            "Kun va vaqtni tushuna olmadim. Iltimos, shu uslubda kiriting:\n"
            "13 1:34:45 PM   yoki   13 13:34:45   yoki   13 13:34"
        )
        return WAITING_NEW_TIME

    context.user_data["new_dt_company_tz"] = new_dt

    # Agar pickup joyining time zone'i avtomatik aniqlangan bo'lsa, kiritilgan
    # vaqtni (company tz bo'yicha) pickup tz'ga konvertatsiya qilamiz
    company_tz = context.user_data.get("company_timezone")
    pickup_iana = context.user_data.get("pickup_iana")
    conversion_note = ""

    if pickup_iana and company_tz:
        try:
            converted_dt = convert_between_timezones(new_dt, company_tz, pickup_iana)
            pickup_abbr = context.user_data.get("pickup_abbr") or iana_to_abbr(pickup_iana, new_dt)
            if converted_dt != new_dt:
                conversion_note = (
                    f"\n\n🕒 Konvertatsiya: {company_tz} {new_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"→ {pickup_abbr} {converted_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            new_dt = converted_dt
        except ValueError:
            pass

    context.user_data["new_dt"] = new_dt

    if mode == "vision":
        # Vision-nomzod uchun: vaqtni ekran ko'rinishidagi shakl bo'yicha (HH:MM yoki HHMM)
        # taxminiy formatlaymiz - eski yozuv qanday ko'ringan bo'lsa shunga moslaymiz
        new_text = _format_like_vision_text(old_raw, new_dt)
    else:
        new_text = format_like_original(new_dt, cand.tm)

    context.user_data["new_text"] = new_text

    keyboard_rows = [
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="confirm_no")],
    ]

    # Agar tanlangan maydon "chiqish/pickup" turkumiga oid bo'lsa va hujjatda
    # mos "kirish" vaqti ham bo'lsa - delta bo'yicha avtomatik moslashtirish taklif qilamiz
    delta_info = _find_paired_in_candidate(context)
    if delta_info is not None:
        context.user_data["delta_pair"] = delta_info
        keyboard_rows.insert(
            0,
            [InlineKeyboardButton(
                "✅ Tasdiqlash + Time In'ni ham mos sur",
                callback_data="confirm_yes_delta",
            )],
        )

    # Agar tanlangan maydon "pickup guruhi"ga oid bo'lsa (SHIP DATE, ORIGIN,
    # Signature va h.k.) - hujjatdagi BOSHQA shu guruh maydonlarini ham
    # birgalikda (mos farq bilan) yangilashni taklif qilamiz
    group_info = _find_pickup_group_candidates(context)
    if group_info:
        context.user_data["pickup_group"] = group_info
        field_names = ", ".join(
            (context.user_data["vision_candidates"][i].context
             if context.user_data.get("mode") == "vision"
             else context.user_data["candidates"][i].context)
            for i, _, _ in group_info
        )
        keyboard_rows.insert(
            0,
            [InlineKeyboardButton(
                f"✅ Tasdiqlash + {len(group_info)} ta bog'liq maydonni ham yangila",
                callback_data="confirm_yes_group",
            )],
        )

    markup = InlineKeyboardMarkup(keyboard_rows)
    extra_note = ""
    if group_info:
        names = "\n".join(
            f"  • {(context.user_data['vision_candidates'][i] if context.user_data.get('mode')=='vision' else context.user_data['candidates'][i]).context}: {raw}"
            for i, _, raw in group_info
        )
        extra_note = f"\n\nBog'liq topilgan maydonlar (xohlasangiz ular ham mos yangilanadi):\n{names}"

    await update.message.reply_text(
        f"Eski qiymat: {old_raw}\n"
        f"Yangi qiymat: {new_text}"
        f"{conversion_note}"
        f"{extra_note}\n\n"
        f"Shu o'zgarishni hujjatga kiritaymi?",
        reply_markup=markup,
    )
    return CONFIRMING


def _format_like_vision_text(old_raw: str, new_dt: datetime) -> str:
    """
    Vision orqali topilgan eski matn ko'rinishiga qarab, yangi vaqtni mos
    formatga soladi. Quyidagi holatlarni qo'llab-quvvatlaydi:
      - faqat vaqt: "1455", "16:45", "4:45 PM", "16:45:30"
      - faqat sana: "06/18/26", "6/19/2026"
      - sana + vaqt: "6/19/2026 06:00"
      - sana + vaqt ORALIG'I: "6/19/2026 06:00-10:00" (davomiylik saqlanadi)
    """
    old_raw = old_raw.strip()

    _MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                     "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Raqamli sana: "6/19/2026" yoki "06-18-26"
    date_m = re.search(r'(\d{1,2})([/\-])(\d{1,2})[/\-](\d{2,4})', old_raw)
    # Amazon uslubidagi sana: "20-Jun-26" (kun-OyQisqaNomi-Yil)
    date_mon_m = None
    if not date_m:
        date_mon_m = re.search(
            r'(\d{1,2})-(' + '|'.join(_MONTHS_SHORT) + r')-(\d{2,4})',
            old_raw, re.IGNORECASE,
        )
    range_m = re.search(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})', old_raw)
    single_time_m = re.search(r'(\d{1,2}):(\d{2})(:(\d{2}))?\s*([AaPp][Mm])?', old_raw) \
        if not range_m else None

    def date_part() -> str:
        if date_mon_m:
            year_len = len(date_mon_m.group(3))
            year_str = str(new_dt.year) if year_len == 4 else str(new_dt.year)[-2:]
            month_name = _MONTHS_SHORT[new_dt.month - 1]
            return f"{new_dt.day}-{month_name}-{year_str}"
        if not date_m:
            return ""
        sep = date_m.group(2)
        year_len = len(date_m.group(4))
        year_str = str(new_dt.year) if year_len == 4 else str(new_dt.year)[-2:]
        return f"{new_dt.month:02d}{sep}{new_dt.day:02d}{sep}{year_str}"

    # date_mon_m topilgan bo'lsa, pastdagi mantiqda "date_m" o'rnida ishlatamiz
    # (faqat sana borligini bildiruvchi bayroq sifatida)
    date_m = date_m or date_mon_m

    # --- 1) sana + vaqt ORALIG'I ---
    if range_m:
        start_h, start_m_, end_h, end_m_ = (int(g) for g in range_m.groups())
        duration = ((end_h * 60 + end_m_) - (start_h * 60 + start_m_)) % (24 * 60)
        new_start_total = new_dt.hour * 60 + new_dt.minute
        new_end_total = (new_start_total + duration) % (24 * 60)
        new_end_h, new_end_m = divmod(new_end_total, 60)
        time_part = f"{new_dt.hour:02d}:{new_dt.minute:02d}-{new_end_h:02d}:{new_end_m:02d}"
        d = date_part()
        return f"{d} {time_part}".strip()

    # --- 2) faqat ajratuvchisiz 4-xonali vaqt (Time In/Out uslubi) - sana yo'q bo'lsa ---
    if not date_m and re.fullmatch(r'\d{4}', old_raw):
        return f"{new_dt.hour:02d}{new_dt.minute:02d}"

    # --- 3) AM/PM bilan vaqt bor (sana bor-yo'qligidan qat'i nazar) ---
    if re.search(r'\d{1,2}:\d{2}(:\d{2})?\s*[AaPp][Mm]', old_raw):
        hour12 = new_dt.hour % 12 or 12
        ampm = "PM" if new_dt.hour >= 12 else "AM"
        has_sec = bool(re.search(r'\d{1,2}:\d{2}:\d{2}\s*[AaPp][Mm]', old_raw))
        time_part = f"{hour12}:{new_dt.minute:02d}"
        if has_sec:
            time_part += f":{new_dt.second:02d}"
        time_part += f" {ampm}"
        d = date_part()
        return f"{d} {time_part}".strip()

    # --- 4) faqat sana, vaqt yo'q ---
    if date_m and not re.search(r'\d{1,2}:\d{2}', old_raw):
        return date_part()

    # --- 5) sana + 24-soat vaqt (AM/PM siz) ---
    if date_m and re.search(r'\d{1,2}:\d{2}', old_raw):
        has_sec = bool(re.search(r'\d{1,2}:\d{2}:\d{2}', old_raw))
        time_part = f"{new_dt.hour:02d}:{new_dt.minute:02d}"
        if has_sec:
            time_part += f":{new_dt.second:02d}"
        d = date_part()
        return f"{d} {time_part}".strip()

    # --- 6) faqat 24-soat vaqt, ajratuvchili, sana yo'q ---
    if re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', old_raw):
        if old_raw.count(":") == 2:
            return f"{new_dt.hour:02d}:{new_dt.minute:02d}:{new_dt.second:02d}"
        return f"{new_dt.hour:02d}:{new_dt.minute:02d}"

    # standart fallback
    return f"{new_dt.hour:02d}:{new_dt.minute:02d}"


def _find_paired_in_candidate(context: ContextTypes.DEFAULT_TYPE):
    """
    Agar tanlangan maydon 'chiqish/pickup' turkumiga oid bo'lsa (Time Out,
    Departure va h.k.) va hujjatda mos 'kirish' (Time In, Arrival) maydoni
    ham topilgan bo'lsa, ularning eski vaqt farqini (delta) hisoblaydi.
    Qaytaradi: (in_candidate_idx, delta_timedelta) yoki None.
    """
    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]

    if mode == "vision":
        all_cands = context.user_data["vision_candidates"]
        chosen = all_cands[idx]
        chosen_ctx = chosen.context.lower()
        old_raw = chosen.raw_text
    else:
        all_cands = context.user_data["candidates"]
        chosen = all_cands[idx]
        chosen_ctx = chosen.context.lower()
        old_raw = chosen.tm.raw_text

    is_out = any(k in chosen_ctx for k in _OUT_KEYWORDS)
    if not is_out:
        return None

    # mos "in" nomzodini qidiramiz
    for other_idx, other in enumerate(all_cands):
        if other_idx == idx:
            continue
        other_ctx = (other.context if mode == "vision" else other.context).lower()
        if any(k in other_ctx for k in _IN_KEYWORDS):
            old_out_dt = _quick_parse_time_text(old_raw)
            old_in_raw = other.raw_text if mode == "vision" else other.tm.raw_text
            old_in_dt = _quick_parse_time_text(old_in_raw)
            if old_out_dt and old_in_dt:
                delta = old_out_dt - old_in_dt
                return (other_idx, delta, old_in_raw)
    return None


def _quick_parse_full_datetime(raw: str, fallback_year: int) -> Optional[datetime]:
    """
    raw matndan SANA+VAQT (yoki faqat sana, yoki faqat vaqt) ni iloji boricha
    to'liq ajratib oladi - pickup-group orasidagi sana/vaqt FARQINI (offset)
    hisoblash uchun ishlatiladi. parse_user_input ga o'xshaydi, lekin bu
    funksiya hujjatdagi ASL (eski) matnlarni o'qish uchun, ko'proq formatga
    chidamli bo'lishi kerak (range, vergul-sana, ko'rinishidagi narsalar ham).
    """
    raw = raw.strip()
    # Avval range bo'lsa - faqat boshlanish qismini olamiz
    raw = re.split(r'\s*-\s*\d{1,2}:\d{2}', raw)[0]

    # to'liq parse_user_input bilan urinib ko'ramiz (sana + vaqt formatlarini biladi)
    dt = parse_user_input(raw, fallback_year=fallback_year)
    if dt:
        return dt

    # faqat sana (vaqtsiz)
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    # faqat vaqt
    return _quick_parse_time_text(raw)


def _find_pickup_group_candidates(context: ContextTypes.DEFAULT_TYPE):
    """
    Agar tanlangan maydon 'pickup guruhi'ga oid bo'lsa (SHIP DATE, ORIGIN,
    Shipper Signature va h.k. - bittasi yuk OLIB KETILISHI voqeasini
    ifodalovchi turli yorliqlar), hujjatdagi BOSHQA shu guruhga oid barcha
    nomzodlarni topadi va har biri uchun eski-vaqtdan FARQNI (timedelta)
    hisoblaydi - shunda yangi vaqt kiritilganda barchasi mos surilishi mumkin.

    Qaytaradi: list of (other_idx, delta_timedelta, old_raw_text) yoki bo'sh list.
    """
    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]

    if mode == "vision":
        all_cands = context.user_data["vision_candidates"]
    else:
        all_cands = context.user_data["candidates"]

    chosen = all_cands[idx]
    chosen_ctx = (chosen.context or "").lower()
    old_raw = chosen.raw_text if mode == "vision" else chosen.tm.raw_text

    is_pickup_group = any(k in chosen_ctx for k in _PICKUP_GROUP_KEYWORDS)
    if not is_pickup_group:
        return []

    fallback_year = _guess_fallback_year(old_raw)
    chosen_dt = _quick_parse_full_datetime(old_raw, fallback_year)
    if chosen_dt is None:
        return []

    results = []
    for other_idx, other in enumerate(all_cands):
        if other_idx == idx:
            continue
        other_ctx = (other.context or "").lower()
        if not any(k in other_ctx for k in _PICKUP_GROUP_KEYWORDS):
            continue
        other_raw = other.raw_text if mode == "vision" else other.tm.raw_text
        other_dt = _quick_parse_full_datetime(other_raw, fallback_year)
        if other_dt is None:
            continue
        delta = other_dt - chosen_dt
        results.append((other_idx, delta, other_raw))

    return results




def _quick_parse_time_text(raw: str) -> Optional[datetime]:
    """Faqat vaqt qismini (HH:MM, HHMM, yoki AM/PM bilan) bugungi sanaga bog'lab datetime'ga aylantiradi (delta hisoblash uchun)."""
    raw = raw.strip()
    today = datetime.now().date()

    # AM/PM bilan: "4:45 PM", "04:45:30 PM"
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(:(\d{2}))?\s*([AaPp][Mm])', raw)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        second = int(m.group(4) or 0)
        ampm = m.group(5).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return datetime(today.year, today.month, today.day, hour, minute, second)

    # ajratuvchisiz HHMM: "1345"
    m = re.fullmatch(r'([01]\d|2[0-3])([0-5]\d)', raw)
    if m:
        return datetime(today.year, today.month, today.day, int(m.group(1)), int(m.group(2)))

    # ajratuvchili 24-soat: "16:45" yoki "16:45:30"
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(:(\d{2}))?', raw)
    if m:
        return datetime(today.year, today.month, today.day, int(m.group(1)), int(m.group(2)), int(m.group(4) or 0))

    return None


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_no":
        if query.message.caption:
            await query.edit_message_caption(caption="Bekor qilindi. Qaytadan boshlash uchun /start bosing.")
        else:
            await query.edit_message_text("Bekor qilindi. Qaytadan boshlash uchun /start bosing.")
        return ConversationHandler.END

    apply_delta = query.data == "confirm_yes_delta"
    apply_group = query.data == "confirm_yes_group"

    await query.message.reply_text("Hujjatni tayyorlayapman...")

    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]
    new_text = context.user_data["new_text"]
    new_dt = context.user_data["new_dt"]

    if mode == "text":
        cand = context.user_data["candidates"][idx]
        doc = fitz.open(stream=context.user_data["original_pdf_bytes"], filetype="pdf")
        replace_text_in_pdf(doc, cand, new_text)

        if apply_group and "pickup_group" in context.user_data:
            for other_idx, delta, old_raw in context.user_data["pickup_group"]:
                other_cand = context.user_data["candidates"][other_idx]
                other_new_dt = new_dt + delta
                other_new_text = format_like_original(other_new_dt, other_cand.tm)
                replace_text_in_pdf(doc, other_cand, other_new_text)

        out_bytes = doc.tobytes()
        doc.close()

    elif mode == "ocr":
        cand = context.user_data["candidates"][idx]
        page_images = context.user_data["page_images"]
        replace_text_in_scanned_pdf(page_images, cand, new_text)

        if apply_group and "pickup_group" in context.user_data:
            for other_idx, delta, old_raw in context.user_data["pickup_group"]:
                other_cand = context.user_data["candidates"][other_idx]
                other_new_dt = new_dt + delta
                other_new_text = format_like_original(other_new_dt, other_cand.tm)
                replace_text_in_scanned_pdf(page_images, other_cand, other_new_text)

        out_bytes = images_to_pdf_bytes(page_images)

    elif mode == "vision":
        page_img = context.user_data["page_image"]
        cand = context.user_data["vision_candidates"][idx]
        replace_vision_candidate_in_image(page_img, cand, new_text)

        if apply_delta and "delta_pair" in context.user_data:
            other_idx, delta, old_in_raw = context.user_data["delta_pair"]
            new_in_dt = new_dt - delta
            new_in_text = _format_like_vision_text(old_in_raw, new_in_dt)
            other_cand = context.user_data["vision_candidates"][other_idx]
            replace_vision_candidate_in_image(page_img, other_cand, new_in_text)

        if apply_group and "pickup_group" in context.user_data:
            for other_idx, delta, old_raw in context.user_data["pickup_group"]:
                other_new_dt = new_dt + delta
                other_new_text = _format_like_vision_text(old_raw, other_new_dt)
                other_cand = context.user_data["vision_candidates"][other_idx]
                replace_vision_candidate_in_image(page_img, other_cand, other_new_text)

        out_bytes = images_to_pdf_bytes([page_img])

    else:
        await query.message.reply_text("Xatolik: noma'lum rejim.")
        context.user_data.clear()
        return ConversationHandler.END

    bio = BytesIO(out_bytes)
    bio.name = "BOL_edited.pdf"
    await query.message.reply_document(document=bio, filename="BOL_edited.pdf")
    await query.message.reply_text(
        "Tayyor! Yana boshqa yozuvni tahrirlash uchun shu faylni qaytadan yuboring "
        "yoki yangi fayl uchun /start bosing."
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.")
    context.user_data.clear()
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOL_BOT_TOKEN muhit o'zgaruvchisi topilmadi. "
            "Telegram bot tokenini BOL_BOT_TOKEN ga o'rnating."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(r"^▶️ Start$"), start),
        ],
        states={
            WAITING_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, receive_file)
            ],
            CHOOSING_FIELD: [
                CallbackQueryHandler(choose_field, pattern=r"^(pick|vpick)_\d+$")
            ],
            CHOOSING_TIMEZONE: [
                CallbackQueryHandler(choose_timezone, pattern=r"^tz_(EDT|CDT|MDT|PDT)$")
            ],
            CHOOSING_MONTH: [
                CallbackQueryHandler(choose_month, pattern=r"^month_\d+$")
            ],
            CHOOSING_YEAR: [
                CallbackQueryHandler(choose_year, pattern=r"^year_\d+$")
            ],
            WAITING_NEW_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_time)
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm, pattern=r"^confirm_(yes|yes_delta|yes_group|no)$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("stop", stop),
            MessageHandler(filters.Regex(r"^⏹ Stop$"), stop),
        ],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.Regex(r"^⏹ Stop$"), stop))
    app.add_handler(MessageHandler(filters.Regex(r"^▶️ Start$"), start))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
