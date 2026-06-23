"""
BOL PDF fayllari bilan ishlash: matn qatlamini o'qish, sana/vaqt
nomzodlarini topish, va topilgan joyni PDF ichida (matn yoki rasm
ustida) yangi qiymat bilan almashtirish.

Ikki rejim qo'llab-quvvatlanadi:
  1) TEXT PDF - PDF ichida haqiqiy matn qatlami bor (PyMuPDF bilan
     to'g'ridan-to'g'ri qidirib, eski matnni oq to'rtburchak bilan
     yopib, ustiga yangi matn yoziladi - "redact & overlay" usuli).
  2) SCANNED PDF - matn qatlami yo'q (rasm sifatida skan qilingan).
     Sahifa rasmga aylantiriladi, OCR (Tesseract) so'z koordinatalari
     bilan o'qiladi, va xuddi shu tarzda eski matn ustiga oq qutича
     chizib, yangi matn yoziladi, so'ngra rasm PDF ga qaytariladi.
"""

import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageFont
import pytesseract

from datetime_utils import find_datetime_candidates, TimeMatch


# Amazon/odatiy BOL hujjatlari deyarli har doim Arial/Helvetica uslubidagi
# shriftda chop etiladi. Liberation Sans Arial bilan METRIK MOS (eni/bo'shliqlari
# bir xil), shuning uchun u eng yaqin moslik beradi. Topilmasa DejaVuSans'ga,
# u ham bo'lmasa PIL default shriftiga tushamiz.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _load_bol_font(fontsize: int) -> "ImageFont.FreeTypeFont":
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, fontsize)
        except Exception:
            continue
    return ImageFont.load_default()


@dataclass
class PageCandidate:
    """Bitta sahifadagi bitta sana/vaqt nomzodi - foydalanuvchiga tanlash uchun ko'rsatiladi."""
    page_index: int
    tm: TimeMatch
    # matn-PDF uchun: fitz.Rect koordinatalari ro'yxati (bir nechta so'z bo'lishi mumkin)
    rects: List["fitz.Rect"]
    context: str  # atrofidagi matn (foydalanuvchiga ko'rsatish uchun, masalan "Pickup Time: ...")
    is_scanned: bool


def is_scanned_pdf(doc: "fitz.Document") -> bool:
    """PDF'da matn qatlami umuman yo'qmi (demak skan/rasm) tekshiradi."""
    total_chars = 0
    for page in doc:
        total_chars += len(page.get_text("text").strip())
        if total_chars > 20:
            return False
    return True


def _get_context(full_text: str, start: int, end: int, radius: int = 30) -> str:
    s = max(0, start - radius)
    e = min(len(full_text), end + radius)
    snippet = full_text[s:e].replace("\n", " ")
    return snippet.strip()


# --------------------------------------------------------------------------
# TEXT PDF rejimi
# --------------------------------------------------------------------------

def extract_text_candidates(doc: "fitz.Document") -> List[PageCandidate]:
    """Matn qatlami bor PDF'dan sana/vaqt nomzodlarini topadi."""
    results: List[PageCandidate] = []
    for page_idx, page in enumerate(doc):
        full_text = page.get_text("text")
        candidates = find_datetime_candidates(full_text)
        for tm in candidates:
            # tm.raw_text bo'yicha sahifada qidirib, koordinatalarini topamiz
            rects = page.search_for(tm.raw_text)
            if not rects:
                # ba'zan ko'p probel/satr-buzilishi tufayli aniq mos kelmasligi mumkin -
                # parchalab qidirib ko'ramiz (sana va vaqt alohida-alohida)
                continue
            ctx = _get_context(full_text, tm.start, tm.end)
            results.append(PageCandidate(
                page_index=page_idx, tm=tm, rects=rects, context=ctx, is_scanned=False
            ))
    return results


def replace_text_in_pdf(doc: "fitz.Document", candidate: PageCandidate, new_text: str) -> None:
    """
    Matnli PDF'da eski sana/vaqt matnini yangisi bilan almashtiradi:
    eski matn PyMuPDF redaction orqali PUTUNLAY o'chiriladi (shunchaki
    ustidan oq chizish yetarli emas - pastki matn qatlami baribir
    PDF ichida qolib ketadi), so'ng o'sha joyga yangi matn yoziladi.
    """
    page = doc[candidate.page_index]

    # Avval barcha rect larni saqlab olamiz (redaction qo'llanganidan keyin
    # ba'zan layout o'zgarishi mumkin, shuning uchun oldindan hisoblaymiz)
    rect_list = list(candidate.rects)

    for rect in rect_list:
        page.add_redact_annot(rect, fill=(1, 1, 1))

    # Redaction'ni qo'llaymiz - bu eski matnni PDF strukturasidan butunlay olib tashlaydi
    page.apply_redactions()

    # Endi bo'shagan joyga yangi matnni yozamiz
    for rect in rect_list:
        fontsize = max(6, min(11, rect.height * 0.72))
        page.insert_text(
            (rect.x0, rect.y1 - rect.height * 0.22),
            new_text,
            fontsize=fontsize,
            fontname="helv",
            color=(0, 0, 0),
        )


# --------------------------------------------------------------------------
# SCANNED PDF rejimi (OCR)
# --------------------------------------------------------------------------

def page_to_image(page: "fitz.Page", zoom: float = 3.0) -> Image.Image:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def extract_ocr_candidates(doc: "fitz.Document") -> Tuple[List[PageCandidate], List[Image.Image]]:
    """
    Skan PDF'dan OCR yordamida sana/vaqt nomzodlarini topadi.
    Har bir sahifa uchun to'liq matnni OCR qiladi, nomzodlarni topadi,
    so'ng har bir nomzodning aniq joylashuvini (bbox) so'z darajasida qayta qidiradi.
    """
    results: List[PageCandidate] = []
    page_images: List[Image.Image] = []

    for page_idx, page in enumerate(doc):
        img = page_to_image(page, zoom=3.0)
        page_images.append(img)

        # So'z darajasidagi OCR ma'lumoti (matn + koordinatalar)
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        words = ocr_data["text"]
        full_text = " ".join(w for w in words if w.strip())

        candidates = find_datetime_candidates(full_text)
        if not candidates:
            continue

        # Har bir nomzod uchun, uning matnini tashkil etuvchi so'zlarni
        # ketma-ket joylashuviga qarab topib, bbox larini yig'amiz.
        # Oddiy va ishonchli usul: nomzod matnini so'zlarga ajratib,
        # OCR so'zlar ketma-ketligida shu ketma-ketlikni qidiramiz.
        n = len(words)
        word_boxes = [
            (words[i], ocr_data["left"][i], ocr_data["top"][i],
             ocr_data["width"][i], ocr_data["height"][i])
            for i in range(n) if words[i].strip()
        ]
        clean_words = [w[0] for w in word_boxes]

        for tm in candidates:
            target_words = tm.raw_text.split()
            match_start = _find_word_sequence(clean_words, target_words)
            if match_start is None:
                continue
            boxes = word_boxes[match_start: match_start + len(target_words)]
            rects = []
            for (_, left, top, width, height) in boxes:
                rects.append(fitz.Rect(left, top, left + width, top + height))
            ctx = _get_context(full_text, tm.start, tm.end)
            results.append(PageCandidate(
                page_index=page_idx, tm=tm, rects=rects, context=ctx, is_scanned=True
            ))

    return results, page_images


def _find_word_sequence(haystack: List[str], needle: List[str]) -> Optional[int]:
    """haystack ichidan needle ketma-ketligini (engil tozalash bilan) qidiradi."""
    def norm(w: str) -> str:
        return w.strip().strip(",.").lower()

    needle_n = [norm(w) for w in needle]
    hl = len(haystack)
    nl = len(needle_n)
    for i in range(hl - nl + 1):
        window = [norm(haystack[i + j]) for j in range(nl)]
        if window == needle_n:
            return i
    return None


def replace_text_in_scanned_pdf(
    page_images: List[Image.Image],
    candidate: PageCandidate,
    new_text: str,
) -> None:
    """Skan sahifa rasmida eski matn ustiga oq quticha chizib, yangi matnni yozadi."""
    from PIL import ImageDraw, ImageFont

    img = page_images[candidate.page_index]
    draw = ImageDraw.Draw(img)

    if not candidate.rects:
        return

    x0 = min(r.x0 for r in candidate.rects)
    y0 = min(r.y0 for r in candidate.rects)
    x1 = max(r.x1 for r in candidate.rects)
    y1 = max(r.y1 for r in candidate.rects)

    draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], fill="white")

    height = y1 - y0
    fontsize = max(10, int(height * 0.85))
    font = _load_bol_font(fontsize)

    draw.text((x0, y0), new_text, fill="black", font=font)


def images_to_pdf_bytes(page_images: List[Image.Image]) -> bytes:
    buf = io.BytesIO()
    if not page_images:
        return b""
    page_images[0].save(buf, format="PDF", save_all=True, append_images=page_images[1:])
    return buf.getvalue()


# --------------------------------------------------------------------------
# VISION (Claude) rejimi - qo'lyozma va past sifatli skanlar uchun
# --------------------------------------------------------------------------

def draw_numbered_overlay(img: Image.Image, vision_candidates: list) -> Image.Image:
    """
    Sahifa rasmiga, Claude vision topgan har bir nomzod atrofiga
    RAQAMLANGAN qizil ramka chizadi - foydalanuvchi shu raqamlardan
    birini tanlab, qaysi yozuvni tahrirlash kerakligini ko'rsatadi.
    """
    from PIL import ImageDraw, ImageFont
    from vision_engine import candidate_to_pixel_rect

    out = img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)
    w, h = out.size

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max(18, h // 80)
        )
    except Exception:
        font = ImageFont.load_default()

    for idx, cand in enumerate(vision_candidates, start=1):
        x0, y0, x1, y1 = candidate_to_pixel_rect(cand, w, h)
        draw.rectangle([x0, y0, x1, y1], outline="red", width=3)
        label = str(idx)
        # Raqamni ramkaning chap-tepa burchagida, ramka CHIZIG'INING TASHQARISIDA,
        # to'q fonli doirachada ko'rsatamiz - matn bilan ustma-ust tushmasligi uchun
        bbox = draw.textbbox((0, 0), label, font=font)
        label_w = bbox[2] - bbox[0]
        label_h = bbox[3] - bbox[1]
        badge_size = max(label_w, label_h) + 14
        bx0 = max(0, x0 - badge_size - 2)
        by0 = max(0, y0)
        bx1 = bx0 + badge_size
        by1 = by0 + badge_size
        draw.ellipse([bx0, by0, bx1, by1], fill="red", outline="white", width=2)
        tx = bx0 + (badge_size - label_w) / 2 - bbox[0]
        ty = by0 + (badge_size - label_h) / 2 - bbox[1]
        draw.text((tx, ty), label, fill="white", font=font)

    return out


def replace_vision_candidate_in_image(
    img: Image.Image,
    vision_candidate,
    new_text: str,
) -> None:
    """
    Vision orqali topilgan (ko'pincha QO'LYOZMA) yozuvni o'chirib,
    o'sha joyga toza, komputer shriftidagi yangi matnni yozadi.
    Eski qo'lyozma butunlay yo'qoladi (foydalanuvchi talabi: "crystal clear").
    """
    from PIL import ImageDraw, ImageFont
    from vision_engine import candidate_to_pixel_rect

    draw = ImageDraw.Draw(img)
    w, h = img.size
    x0, y0, x1, y1 = candidate_to_pixel_rect(vision_candidate, w, h)

    # Eski yozuvni (qo'lyozma yoki bosma) oq fon bilan to'liq yopamiz
    draw.rectangle([x0, y0, x1, y1], fill="white")

    # Yangi matnni komputer shriftida, aniq va tekis yozamiz
    box_height = y1 - y0
    fontsize = max(10, int(box_height * 0.78))
    font = _load_bol_font(fontsize)

    draw.text((x0 + 2, y0 + (box_height * 0.08)), new_text, fill="black", font=font)
