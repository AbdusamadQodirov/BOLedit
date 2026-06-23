"""
Gibrid vision moduli: Claude (Anthropic API) + Tesseract kombinatsiyasi.

Yangi arxitektura (koordinata aniqligi muammosini hal qiladi):
  1) Claude vision  → faqat MATN va KONTEKST o'qiydi (koordinata BERMAYDI)
                      - qo'lyozmani ham yaxshi taniydi
  2) Tesseract OCR  → barcha so'zlar uchun ANIQ PIKSEL koordinatalari beradi
  3) Matching       → Claude topgan matnni Tesseract so'zlar ichida qidirib,
                      ANIQ PIKSEL joyini topadi

Natijada: Claude aniqligi + Tesseract koordinata aniqligi = to'g'ri joy.
"""

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pytesseract
from PIL import Image
from anthropic import Anthropic

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY muhit o'zgaruvchisi topilmadi."
            )
        _client = Anthropic(api_key=api_key)
    return _client


@dataclass
class VisionCandidate:
    """Gibrid usul orqali topilgan bitta sana/vaqt nomzodi."""
    raw_text: str          # Claude o'qigan matn
    is_handwritten: bool
    confidence: str        # "high" | "medium" | "low"
    context: str           # qaysi maydon ekanligi, masalan "Schedule Departure Date"
    # Aniq piksel koordinatalari (Tesseract orqali topilgan):
    px0: float = 0.0
    py0: float = 0.0
    px1: float = 0.0
    py1: float = 0.0
    # Agar Tesseract mos joy topa olmasa, foiz-asosida zahira saqlanadi:
    x_pct: float = 0.0
    y_pct: float = 0.0
    width_pct: float = 5.0
    height_pct: float = 2.0
    matched_by_ocr: bool = False  # True = piksel koordinatalari ishonchli


# --------------------------------------------------------------------------
# 1-QISM: Claude vision — faqat matn + kontekst so'raydi
# --------------------------------------------------------------------------

_SYSTEM_PROMPT_TEXT_ONLY = """Sen logistika hujjatlari (Bill of Lading - BOL) bilan ishlaydigan matn tahlilchisan.
Senga BOL hujjatining bir sahifasi rasm sifatida beriladi.

VAZIFA: rasmdagi BARCHA sana va/yoki vaqt yozuvlarini top - bosma (kompyuterda
chop etilgan) bo'lsin, qo'lyozma (qo'lda yozilgan) bo'lsin, farqi yo'q.

Har bir topilgan yozuv uchun quyidagilarni aniqla:
- text: aynan o'qigan matn (masalan "23:15", "1345", "06/18/26", "01:45")
  MUHIM: matnni AYNAN rasmda ko'ringan kabi yoz - hech narsa qo'shma, hech narsa o'zgartiirma
- is_handwritten: true yoki false
- confidence: "high" / "medium" / "low"  
- context: bu raqam qaysi yorliq yonida yoki qaysi maydonda joylashgani
  (masalan "Schedule Departure Date", "Print Date", "Time In", "Time Out",
  "Appointment Time", "Date", "Equip Arrival", "Shipper Signature Date")
  MUHIM: context matnini rasmda ko'ringan YORLIQ matnidan ol, ixtiro qilma

FAQAT quyidagi JSON formatida javob ber (bbox, koordinata KERAK EMAS - faqat matn):

{
  "candidates": [
    {"text": "23:15", "is_handwritten": false, "confidence": "high", "context": "Schedule Departure Date"},
    {"text": "01:45", "is_handwritten": true, "confidence": "high", "context": "handwritten note"},
    ...
  ]
}"""


def _image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def _ask_claude_for_texts(img: Image.Image, model: str = "claude-sonnet-4-6") -> List[dict]:
    """Claude vision'dan faqat sana/vaqt matnlarini so'raydi (koordinatasiz)."""
    client = _get_client()
    img_b64 = _image_to_base64(img)

    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=_SYSTEM_PROMPT_TEXT_ONLY,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                },
                {
                    "type": "text",
                    "text": "Ushbu BOL sahifasidagi barcha sana/vaqt yozuvlarini top. "
                            "Faqat JSON qaytar, koordinata SHART EMAS.",
                },
            ],
        }],
    )

    text = "\n".join(b.text for b in response.content if hasattr(b, "text")).strip()
    # markdown fence tozalash
    text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^```\s*', '', text)
    text = text.rstrip('`').strip()

    try:
        return json.loads(text).get("candidates", [])
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------
# 2-QISM: Tesseract — barcha so'z koordinatalari
# --------------------------------------------------------------------------

def _get_tesseract_words(img: Image.Image, min_conf: int = 20) -> List[dict]:
    """
    Tesseract orqali rasmdan barcha so'zlarni va ularning piksel
    koordinatalarini oladi. min_conf dan past ishonchli so'zlar ham
    kiritiladi (chunki matching jarayonida Claude matnimiz aniq bo'ladi).
    """
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = []
    for i, (text, conf) in enumerate(zip(data["text"], data["conf"])):
        if not text.strip():
            continue
        try:
            conf_int = int(conf)
        except (ValueError, TypeError):
            conf_int = 0
        if conf_int < min_conf:
            continue
        words.append({
            "text": text.strip(),
            "conf": conf_int,
            "x": data["left"][i],
            "y": data["top"][i],
            "w": data["width"][i],
            "h": data["height"][i],
        })
    return words


# --------------------------------------------------------------------------
# 3-QISM: Matching — Claude matni → Tesseract koordinatalari
# --------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Matching uchun matnni normallashtiradi (faqat raqam/harf/ikki nuqta)."""
    return re.sub(r'[^a-zA-Z0-9:./\-]', '', s).lower()


def _words_to_phrase_candidates(words: List[dict], max_span: int = 4) -> List[dict]:
    """
    Tesseract so'zlarini birlashtirib, 1..max_span so'zdan iborat
    "iboralar" ro'yxatini yaratadi. Bu "23:15" kabi ko'p so'z sifatida
    o'qilgan vaqtlarni ham topishga imkon beradi.
    """
    phrases = []
    n = len(words)
    for i in range(n):
        for j in range(i + 1, min(i + max_span + 1, n + 1)):
            span = words[i:j]
            # Birlashtirilgan matn
            combined = " ".join(w["text"] for w in span)
            # Birlashtirilgan bbox
            x0 = min(w["x"] for w in span)
            y0 = min(w["y"] for w in span)
            x1 = max(w["x"] + w["w"] for w in span)
            y1 = max(w["y"] + w["h"] for w in span)
            phrases.append({
                "text": combined,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "conf": min(w["conf"] for w in span),
            })
    return phrases


def _find_best_ocr_match(
    target: str,
    phrases: List[dict],
    img_width: int,
    img_height: int,
    context_hint: str = "",
) -> Optional[Tuple[float, float, float, float]]:
    """
    Claude topgan 'target' matnini Tesseract iboralari ichida qidiradi.
    Eng yaqin moslikni (piksel koordinatalar) qaytaradi, yoki None.

    context_hint: Claude bergan kontekst matni (masalan "Schedule Departure Date").
    Agar context_hint berilsa, Tesseract so'zlari ichida kontekst so'zlariga
    YAQIN joylashgan nomzodlarga YUQORI USTUNLIK beriladi - bu "23:15" va
    boshqa "23:xx" kabi o'xshash raqamlarni to'g'ri ajratishga yordam beradi.

    Moslik algoritmi (ustuvorlik bo'yicha):
    1. Aynan mos (normalized) + kontekst yaqinligi
    2. Target iborasi ichida bor (substring)
    3. Raqam/vaqt qismlarining kesishuvi + kontekst yaqinligi
    """
    t_norm = _normalize(target)
    if not t_norm:
        return None

    # Kontekst so'zlaridan Tesseract ichidagi yaqin "kontekst hududi"ni topamiz
    context_region: Optional[Tuple[int, int, int, int]] = None
    if context_hint.strip():
        ctx_words = [_normalize(w) for w in context_hint.split() if len(w) > 2]
        ctx_matches = []
        for ph in phrases:
            if any(cw in _normalize(ph["text"]) for cw in ctx_words if cw):
                ctx_matches.append(ph)
        if ctx_matches:
            # Kontekst so'zlari topilgan hududning bbox'ini topamiz
            cx0 = min(p["x0"] for p in ctx_matches)
            cy0 = min(p["y0"] for p in ctx_matches)
            cx1 = max(p["x1"] for p in ctx_matches)
            cy1 = max(p["y1"] for p in ctx_matches)
            # Qidiruv hududi: kontekst yaqinida, bir qator pastgacha
            row_h = max((p["y1"] - p["y0"]) for p in ctx_matches) * 2
            context_region = (cx0 - 50, cy0 - 5, cx1 + 50, cy1 + row_h + 20)

    best: Optional[dict] = None
    best_score = -1

    for phrase in phrases:
        p_norm = _normalize(phrase["text"])
        if not p_norm:
            continue

        score = 0
        if t_norm == p_norm:
            score = 100
        elif t_norm in p_norm:
            score = 80 - (len(p_norm) - len(t_norm)) * 2
        elif p_norm in t_norm:
            score = 70 - (len(t_norm) - len(p_norm)) * 2
        else:
            common_prefix = 0
            for a, b in zip(t_norm, p_norm):
                if a == b:
                    common_prefix += 1
                else:
                    break
            if common_prefix >= max(2, len(t_norm) * 0.4):
                score = 30 + common_prefix * 3

        if score <= 0:
            continue

        # Kontekst hududiga yaqin bo'lsa, ball oshiramiz
        if context_region:
            rx0, ry0, rx1, ry1 = context_region
            px_mid = (phrase["x0"] + phrase["x1"]) / 2
            py_mid = (phrase["y0"] + phrase["y1"]) / 2
            if rx0 <= px_mid <= rx1 and ry0 <= py_mid <= ry1:
                score += 50  # Kontekst hududida — kuchli bonus

        if score > best_score:
            best_score = score
            best = phrase

    if best is None or best_score < 20:
        return None

    pad = max(2, (best["y1"] - best["y0"]) * 0.1)
    return (
        max(0, best["x0"] - pad),
        max(0, best["y0"] - pad),
        min(img_width, best["x1"] + pad),
        min(img_height, best["y1"] + pad),
    )


# --------------------------------------------------------------------------
# ASOSIY FUNKSIYA: gibrid topuvchi
# --------------------------------------------------------------------------

def find_vision_candidates(img: Image.Image, model: str = "claude-sonnet-4-6") -> List[VisionCandidate]:
    """
    Gibrid usul:
    1) Claude vision → sana/vaqt matnlari + kontekst (koordinatasiz)
    2) Tesseract     → barcha so'z koordinatalari
    3) Matching      → har bir Claude topgan matni uchun OCR'dan aniq piksel joy topiladi

    Agar biror matn uchun OCR mos joy topa olmasa, u baribir ro'yxatga
    kiritiladi (matched_by_ocr=False) va foydalanuvchi raqam orqali tanlaydi,
    lekin joylashtirish uchun qo'shimcha aniqlik so'raladi.
    """
    # 1) Claude: faqat matn
    raw_items = _ask_claude_for_texts(img, model=model)

    # 2) Tesseract: so'z koordinatalari
    tess_words = _get_tesseract_words(img, min_conf=20)
    img_w, img_h = img.size
    phrases = _words_to_phrase_candidates(tess_words, max_span=5)

    # 3) Har bir Claude topgan matn uchun OCR'dan joy qidirish
    results: List[VisionCandidate] = []
    for item in raw_items:
        text = str(item.get("text", "")).strip()
        if not text:
            continue

        pixel_rect = _find_best_ocr_match(
            text, phrases, img_w, img_h,
            context_hint=str(item.get("context", "")),
        )

        cand = VisionCandidate(
            raw_text=text,
            is_handwritten=bool(item.get("is_handwritten", False)),
            confidence=str(item.get("confidence", "low")),
            context=str(item.get("context", "")),
        )

        if pixel_rect:
            cand.px0, cand.py0, cand.px1, cand.py1 = pixel_rect
            cand.matched_by_ocr = True
        else:
            # OCR mos joy topa olmadi — foiz zahira sifatida bo'sh qoldiramiz
            cand.matched_by_ocr = False

        results.append(cand)

    return results


def candidate_to_pixel_rect(
    cand: VisionCandidate,
    img_width: int,
    img_height: int,
) -> Tuple[float, float, float, float]:
    """
    VisionCandidate dan piksel (x0, y0, x1, y1) qaytaradi.
    Agar OCR orqali aniq joy topilgan bo'lsa (matched_by_ocr=True),
    shu piksel koordinatalarini qaytaradi. Aks holda foiz asosida hisoblaydi
    (kam ishonchli, lekin yaxshidan ko'ra yaxshirog'i).
    """
    if cand.matched_by_ocr and cand.px1 > cand.px0:
        return (cand.px0, cand.py0, cand.px1, cand.py1)
    # Zahira: foiz asosida
    x0 = max(0, (cand.x_pct / 100.0) * img_width - 4)
    y0 = max(0, (cand.y_pct / 100.0) * img_height - 4)
    x1 = min(img_width, x0 + (cand.width_pct / 100.0) * img_width + 8)
    y1 = min(img_height, y0 + (cand.height_pct / 100.0) * img_height + 8)
    return (x0, y0, x1, y1)
