"""
BOL (Bill of Lading) Pickup Time Tahrirlovchi Telegram Bot
=============================================================

OQIM:
1. /start -> foydalanuvchi BOL fayl yuboradi (PDF yoki rasm: jpg/png)
2. Bot hujjat turini aniqlaydi:
   a) Matn qatlami bor PDF -> to'g'ridan-to'g'ri matn orqali qidiriladi
   b) Skan/rasm -> avval Tesseract OCR bilan tez qidiriladi (bosma matn
      uchun yaxshi ishlaydi)
   c) OCR yetarlicha topa olmasa (masalan QO'LYOZMA yozuvlar) -> Claude
      vision (Anthropic API) orqali rasm tahlil qilinadi, bu qo'lyozmani
      ham yaxshi taniydi
3. Topilgan barcha nomzodlar RAQAMLANGAN qizil ramka bilan rasmda
   ko'rsatiladi (vision/OCR yo'li uchun) yoki tugmalar ro'yxati
   sifatida (matnli PDF yo'li uchun)
4. Foydalanuvchi qaysi yozuvni tahrirlash kerakligini tanlaydi
5. Bot ELD asosidagi yangi (to'g'ri) vaqtni so'raydi
6. Yangi vaqt eski yozuv FORMATIGA moslab generatsiya qilinadi:
   - 24-soat/AM-PM, sekund bor/yo'q, sana uslubi - barchasi saqlanadi
   - QO'LYOZMA yozuvlar uchun: eski yozuv butunlay o'chiriladi va
     o'rniga TOZA, komputer shriftidagi yangi matn yoziladi
7. MAXSUS LOGIKA: agar tanlangan maydon "Time Out" (yoki shunga
   o'xshash pickup-chiqish vaqti) bo'lsa va hujjatda "Time In" ham
   topilgan bo'lsa, Time In ham xuddi shu farq (delta) bilan
   avtomatik suriladi
8. Tayyor hujjat (PDF) foydalanuvchiga qaytariladi

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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from vision_engine import find_vision_candidates, VisionCandidate

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOL_BOT_TOKEN", "")

# Conversation holatlari
(
    WAITING_FILE, CHOOSING_FIELD, WAITING_NEW_TIME,
    CONFIRMING, ASK_DELTA_CONFIRM,
) = range(5)

# Vaqt maydoni nomida shu so'zlardan biri bo'lsa, "chiqish/pickup vaqti" deb hisoblanadi
_OUT_KEYWORDS = ("time out", "pickup", "departure", "depart", "out")
_IN_KEYWORDS = ("time in", "arrival", "check in", "checkin", " in")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Salom! Men BOL (Bill of Lading) hujjatidagi pickup vaqtini "
        "ELD ma'lumotiga moslab to'g'irlashga yordam beraman.\n\n"
        "BOL faylini yuboring - PDF yoki rasm (foto) shaklida bo'lishi mumkin."
    )
    return WAITING_FILE


def _pil_from_bytes(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGB")


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDF yoki rasm (jpg/png) faylini qabul qiladi."""
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
        # Rasmdan bitta sahifali "PDF" yasaymiz, qolgan oqim bir xil ishlashi uchun
        pix_img = _pil_from_bytes(file_bytes)
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

    await query.message.reply_text(
        f"Tanlandi: {old_text}  ({context_label})\n\n"
        f"Endi ELD logbook'dagi HAQIQIY vaqtni kiriting.\n"
        f"Masalan: July 13, 1:34:45 PM\n"
        f"yoki: 07/13/2026 13:34:45\n"
        f"yoki shunchaki: 13:34"
    )
    return WAITING_NEW_TIME


def _guess_fallback_year(text: str) -> int:
    ym = re.search(r'(20\d{2})', text)
    if ym:
        return int(ym.group(1))
    return datetime.now().year


async def receive_new_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]

    if mode == "vision":
        cand = context.user_data["vision_candidates"][idx]
        old_raw = cand.raw_text
        fallback_year = _guess_fallback_year(old_raw)
    else:
        cand = context.user_data["candidates"][idx]
        old_raw = cand.tm.raw_text
        fallback_year = _guess_fallback_year(old_raw)

    new_dt = parse_user_input(text, fallback_year=fallback_year)
    if new_dt is None:
        await update.message.reply_text(
            "Vaqtni tushuna olmadim. Iltimos, shu uslubda kiriting:\n"
            "July 13, 1:34:45 PM  yoki  07/13/2026 13:34:45  yoki  13:34"
        )
        return WAITING_NEW_TIME

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

    markup = InlineKeyboardMarkup(keyboard_rows)
    await update.message.reply_text(
        f"Eski qiymat: {old_raw}\n"
        f"Yangi qiymat: {new_text}\n\n"
        f"Shu o'zgarishni hujjatga kiritaymi?",
        reply_markup=markup,
    )
    return CONFIRMING


def _format_like_vision_text(old_raw: str, new_dt: datetime) -> str:
    """
    Vision orqali topilgan eski matn ko'rinishiga (ajratuvchili HH:MM yoki
    ajratuvchisiz HHMM) qarab, yangi vaqtni mos formatga soladi.
    """
    if re.fullmatch(r'\d{4}', old_raw.strip()):
        return f"{new_dt.hour:02d}{new_dt.minute:02d}"
    if re.search(r'\d{1,2}:\d{2}(:\d{2})?\s*[AaPp][Mm]', old_raw):
        hour12 = new_dt.hour % 12 or 12
        ampm = "PM" if new_dt.hour >= 12 else "AM"
        if ":" in old_raw and old_raw.count(":") == 2:
            return f"{hour12}:{new_dt.minute:02d}:{new_dt.second:02d} {ampm}"
        return f"{hour12}:{new_dt.minute:02d} {ampm}"
    if re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', old_raw.strip()):
        if old_raw.count(":") == 2:
            return f"{new_dt.hour:02d}:{new_dt.minute:02d}:{new_dt.second:02d}"
        return f"{new_dt.hour:02d}:{new_dt.minute:02d}"
    if re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', old_raw):
        # sana formatida - kun/oy/yil saqlanadi
        m = re.search(r'(\d{1,2})([/\-])(\d{1,2})[/\-](\d{2,4})', old_raw)
        if m:
            sep = m.group(2)
            year_len = len(m.group(4))
            year_str = str(new_dt.year) if year_len == 4 else str(new_dt.year)[-2:]
            return f"{new_dt.month:02d}{sep}{new_dt.day:02d}{sep}{year_str}"
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

    await query.message.reply_text("Hujjatni tayyorlayapman...")

    mode = context.user_data.get("mode")
    idx = context.user_data["chosen_idx"]
    new_text = context.user_data["new_text"]
    new_dt = context.user_data["new_dt"]

    if mode == "text":
        cand = context.user_data["candidates"][idx]
        doc = fitz.open(stream=context.user_data["original_pdf_bytes"], filetype="pdf")
        replace_text_in_pdf(doc, cand, new_text)
        out_bytes = doc.tobytes()
        doc.close()

    elif mode == "ocr":
        cand = context.user_data["candidates"][idx]
        page_images = context.user_data["page_images"]
        replace_text_in_scanned_pdf(page_images, cand, new_text)
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
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, receive_file)
            ],
            CHOOSING_FIELD: [
                CallbackQueryHandler(choose_field, pattern=r"^(pick|vpick)_\d+$")
            ],
            WAITING_NEW_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_time)
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm, pattern=r"^confirm_(yes|yes_delta|no)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
