"""
Foto orqali yuklangan BOL hujjatining qora fon/stol kabi ortiqcha
joylarini olib tashlab, faqat qog'ozning o'zini (to'g'ri burchakka
tekislangan holda) ajratib oladi.

Usul: OpenCV orqali eng katta 4-burchakli konturni (taxminan hujjat
chetlarini) topamiz, so'ng perspektiv transformatsiya (4-nuqta warp)
qo'llab, hujjatni "yuqoridan tik qaragandek" holatga keltiramiz.

Agar kontur ishonchli topilmasa (masalan fon juda murakkab yoki
hujjat chetlari aniq ko'rinmasa), ASL rasm o'zgarishsiz qaytariladi -
bu xavfsiz fallback, hech narsa buzilmasligi uchun.
"""

import numpy as np
import cv2
from PIL import Image


def _order_points(pts: np.ndarray) -> np.ndarray:
    """4 nuqtani [top-left, top-right, bottom-right, bottom-left] tartibida joylashtiradi."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _four_point_transform(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    rect = _order_points(pts)
    (tl, tr, br, bl) = rect

    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    if max_width < 10 or max_height < 10:
        return image  # noto'g'ri o'lcham - xavfsizlik uchun asl rasmni qaytaramiz

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1],
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, M, (max_width, max_height))


def _find_document_contour(image: np.ndarray) -> "Optional[np.ndarray]":
    """
    Rasmda hujjatga mos keluvchi eng katta 4-burchakli konturni qidiradi.
    Topilsa, ASL rasm o'lchamidagi 4 ta (x,y) koordinatani qaytaradi.
    Topilmasa None qaytaradi.
    """
    h, w = image.shape[:2]
    ratio = 1000.0 / max(h, w) if max(h, w) > 1000 else 1.0
    small = cv2.resize(image, (int(w * ratio), int(h * ratio)))

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur = cv2.bilateralFilter(gray, 9, 75, 75)
    edged = cv2.Canny(blur, 30, 100)
    edged = cv2.dilate(edged, np.ones((3, 3), np.uint8), iterations=2)
    edged = cv2.erode(edged, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]

    small_area = small.shape[0] * small.shape[1]

    for c in contours:
        area = cv2.contourArea(c)
        if area < small_area * 0.2:
            continue  # juda kichik - hujjat bo'lishi dargumon
        peri = cv2.arcLength(c, True)
        for eps_mult in (0.01, 0.02, 0.03, 0.04):
            approx = cv2.approxPolyDP(c, eps_mult * peri, True)
            if len(approx) == 4:
                return (approx.reshape(4, 2) / ratio).astype("float32")

    return None


def auto_crop_document(img: Image.Image) -> Image.Image:
    """
    PIL rasmni qabul qiladi, hujjat chetlarini aniqlab, perspektivani
    to'g'rilab qaytaradi. Agar chetlar ishonchli aniqlanmasa, ASL rasmni
    o'zgarishsiz qaytaradi (xavfsiz fallback).
    """
    try:
        cv_img = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
        contour = _find_document_contour(cv_img)
        if contour is None:
            return img
        warped = _four_point_transform(cv_img, contour)
        warped_rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)
        return Image.fromarray(warped_rgb)
    except Exception:
        # Har qanday kutilmagan xatoda ham asl rasmni buzmasdan qaytaramiz
        return img


def auto_rotate_document(img: Image.Image) -> Image.Image:
    """
    Hujjatdagi matn yo'nalishini (0/90/180/270 gradus) Tesseract OSD
    (Orientation and Script Detection) yordamida aniqlab, matn TO'G'RI
    (vertikal o'qiladigan) holatga kelguncha aylantiradi.

    Agar aniqlash muvaffaqiyatsiz bo'lsa (masalan matn juda kam yoki
    OSD ishonchsiz), asl rasmni o'zgarishsiz qaytaradi.
    """
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        rotate_by = osd.get("rotate", 0)
        if rotate_by and rotate_by != 0:
            # PIL.rotate() soat strelkasiga TESKARI yo'nalishda aylantiradi,
            # Tesseract "rotate" qiymati esa soat yo'nalishida qancha aylantirish
            # kerakligini bildiradi - shuning uchun minus belgisi bilan beramiz
            return img.rotate(-rotate_by, expand=True)
    except Exception:
        pass
    return img
