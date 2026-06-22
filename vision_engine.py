"""
Claude (Anthropic API) vision orqali BOL sahifa rasmidan sana/vaqt
yozuvlarini topish. Bu Tesseract OCR yetarli bo'lmagan holatlar uchun
(ayniqsa QO'LYOZMA raqamlar) ishlatiladi - Claude vision qo'lyozmani
ancha yaxshi taniydi.

Claude'dan so'raladigan narsa: rasmdagi BARCHA sana/vaqtga o'xshagan
yozuvlarni (bosma ham, qo'lyozma ham, hatto noaniq bo'lsa ham) topib,
har biri uchun:
  - eng yaqin o'qilgan matn (agar aniq o'qilmasa - eng yaqin taxmin)
  - rasmdagi taxminiy joylashuvi (foiz koordinatasi: 0-100 oralig'ida,
    chunki rasm o'lchamini Claude aniq bilmaydi, lekin nisbiy joyni
    yaxshi baholay oladi)
  - bu yozuv qo'lyozmami yoki bosma matnmi
  - atrofidagi kontekst (qaysi maydon ekanligi, masalan "Time Out")
JSON formatida qaytarishni so'raymiz.
"""

import base64
import io
import json
import os
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image
from anthropic import Anthropic

# API key muhit o'zgaruvchisidan olinadi
_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY muhit o'zgaruvchisi topilmadi. "
                "Claude vision orqali qo'lyozma aniqlash uchun bu kerak."
            )
        _client = Anthropic(api_key=api_key)
    return _client


@dataclass
class VisionCandidate:
    """Claude vision topgan bitta sana/vaqt nomzodi."""
    raw_text: str            # Claude o'qigan matn (masalan "1345" yoki "1455" yoki "noaniq, taxminan 14:xx")
    is_handwritten: bool
    confidence: str          # "high" | "medium" | "low"
    context: str             # qaysi maydon ekanligi haqida qisqa izoh, masalan "Time Out"
    # Joylashuv - rasm o'lchamiga nisbatan FOIZ (0-100) koordinatalari:
    x_pct: float
    y_pct: float
    width_pct: float
    height_pct: float


_SYSTEM_PROMPT = """Sen logistika hujjatlari (Bill of Lading - BOL) bilan ishlaydigan aniq vizual tahlilchisan.
Senga BOL hujjatining bir sahifasi rasm sifatida beriladi.

VAZIFA: rasmdagi BARCHA sana va/yoki vaqt yozuvlarini top - bosma (kompyuterda
chop etilgan) bo'lsin, qo'lyozma (qo'lda yozilgan) bo'lsin, farqi yo'q. Hatto
yozuv noaniq yoki qisman o'qib bo'lmaydigan bo'lsa ham, uni ro'yxatga qo'sh va
eng yaqin taxminingni yoz, confidence="low" deb belgila.

Har bir topilgan yozuv uchun quyidagilarni aniqla:
- text: o'qigan matning (masalan "1345", "06/18/26", "1455", "1500")
- is_handwritten: true/false
- confidence: "high" / "medium" / "low"
- context: bu raqam qaysi maydonga tegishli ekanligi (yorliq matnidan, masalan
  "Time In", "Time Out", "Appointment Time", "Date", "Equip Arrival" va h.k.)
- bbox: rasm ichidagi joylashuvi, FOIZ (0-100) shaklida:
    x (chap chetidan necha foiz), y (yuqori chetidan necha foiz),
    width (yozuv eni necha foiz), height (yozuv balandligi necha foiz)
  Bu yerda butun rasm kengligi/balandligi 100% deb olinadi.

FAQAT quyidagi JSON formatida javob ber, boshqa hech narsa yozma (izoh, markdown belgilari ham kerak emas):

{
  "candidates": [
    {"text": "1345", "is_handwritten": true, "confidence": "high", "context": "Time In", "bbox": {"x": 12.5, "y": 91.2, "width": 6.0, "height": 2.0}},
    ...
  ]
}
"""


def _image_to_base64(img: Image.Image) -> str:
    """Rasmni base64 ga aylantiradi. API limiti uchun max 1568px ga kichraytiramiz."""
    MAX_DIM = 1568
    w, h = img.size
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def find_vision_candidates(img: Image.Image, model: str = "claude-sonnet-4-6") -> List[VisionCandidate]:
    """
    Berilgan sahifa rasmini Claude vision orqali tahlil qilib,
    barcha sana/vaqt nomzodlarini (bosma + qo'lyozma) qaytaradi.
    """
    client = _get_client()
    img_b64 = _image_to_base64(img)

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Ushbu BOL sahifasidagi barcha sana/vaqt yozuvlarini top va JSON qaytar.",
                    },
                ],
            }
        ],
    )

    # Javobdan matnni yig'amiz
    text_parts = [block.text for block in response.content if hasattr(block, "text")]
    full_text = "\n".join(text_parts).strip()

    # Ba'zan model ```json fence bilan o'rab yuborishi mumkin - tozalaymiz
    if full_text.startswith("```"):
        full_text = full_text.strip("`")
        if full_text.lower().startswith("json"):
            full_text = full_text[4:].strip()

    try:
        data = json.loads(full_text)
    except json.JSONDecodeError:
        return []

    results = []
    for item in data.get("candidates", []):
        bbox = item.get("bbox", {})
        try:
            results.append(VisionCandidate(
                raw_text=str(item.get("text", "")).strip(),
                is_handwritten=bool(item.get("is_handwritten", False)),
                confidence=str(item.get("confidence", "low")),
                context=str(item.get("context", "")),
                x_pct=float(bbox.get("x", 0)),
                y_pct=float(bbox.get("y", 0)),
                width_pct=float(bbox.get("width", 5)),
                height_pct=float(bbox.get("height", 2)),
            ))
        except (ValueError, TypeError):
            continue

    return results


def candidate_to_pixel_rect(cand: VisionCandidate, img_width: int, img_height: int):
    """Foiz koordinatalarini piksel (x0, y0, x1, y1) ga aylantiradi, biroz padding bilan."""
    x0 = max(0, (cand.x_pct / 100.0) * img_width - 4)
    y0 = max(0, (cand.y_pct / 100.0) * img_height - 4)
    x1 = min(img_width, x0 + (cand.width_pct / 100.0) * img_width + 8)
    y1 = min(img_height, y0 + (cand.height_pct / 100.0) * img_height + 8)
    return (x0, y0, x1, y1)
