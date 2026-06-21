"""
BOL hujjatlaridagi sana/vaqt matnlarini topish, parse qilish va
asl formatga moslab qayta generatsiya qilish uchun yordamchi funksiyalar.

Asosiy g'oya:
- PDF ichidan topilgan eski matn (masalan "07/13/2026 16:45" yoki
  "Jul 13, 2026 4:45 PM") qanday formatda yozilgan bo'lsa,
  foydalanuvchi kiritgan yangi vaqt ham xuddi SHU formatga solinadi.
- Buning uchun avval eski matnning "shablonini" (pattern) aniqlaymiz:
    * 24-soatlikmi yoki AM/PM bormi
    * sekund bormi yo'qmi
    * sana formati qanday (MM/DD/YYYY, "Jul 13, 2026", "2026-07-13" va h.k.)
    * ajratuvchilar qanday (/, -, probel, vergul)
"""

import re
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class TimeMatch:
    """PDF ichidan topilgan bitta sana/vaqt yozuvi haqida ma'lumot."""
    raw_text: str          # topilgan asl matn, masalan "07/13/2026 04:45 PM"
    start: int              # qatordagi boshlanish indeksi (matn ichida qidirish uchun)
    end: int
    has_date: bool
    has_time: bool
    has_seconds: bool
    has_ampm: bool
    date_style: str          # "mdy_slash" | "month_name" | "iso" | "dmy_slash" | "unknown"
    month_style: Optional[str]  # "short" ("Jul") | "long" ("July") | None
    sep_date: str             # "/", "-", " "
    sep_time: str             # ":"
    upper_ampm: bool          # "PM" vs "pm"


# Oy nomlari (qisqa va to'liq), parsing uchun
_MONTHS_LONG = ["January","February","March","April","May","June","July",
                "August","September","October","November","December"]
_MONTHS_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_MONTH_TO_NUM = {m.lower(): i+1 for i, m in enumerate(_MONTHS_LONG)}
_MONTH_TO_NUM.update({m.lower(): i+1 for i, m in enumerate(_MONTHS_SHORT)})


# --------------------------------------------------------------------------
# 1) PDF matnidan sana/vaqt "nomzodlarini" qidirish
# --------------------------------------------------------------------------

# Turli xil sana+vaqt kombinatsiyalarini qamrab oluvchi regexlar.
# Har biri (pattern, date_style, month_style) qaytaradi.
_PATTERNS = [
    # 1) YYYY-MM-DD HH:MM[:SS] [AM/PM]   ISO uslub (eng aniq, birinchi tekshiriladi)
    (re.compile(
        r'(?P<date>\d{4}-\d{2}-\d{2})'
        r'(?P<sep>[ T,]+)'
        r'(?P<time>\d{1,2}:\d{2}(:\d{2})?)'
        r'(?P<ampm>\s*[AaPp]\.?[Mm]\.?)?'
    ), "iso", None),

    # 2) DD-Mon-YY HH:MM[:SS] [AM/PM]   masalan "20-Jun-26 23:15" (Amazon)
    (re.compile(
        r'(?P<date>\d{1,2}-(' + '|'.join(_MONTHS_SHORT) + r')-\d{2,4})'
        r'(?P<sep>[ ,]+)'
        r'(?P<time>\d{1,2}:\d{2}(:\d{2})?)'
        r'(?P<ampm>\s*[AaPp]\.?[Mm]\.?)?'
    ), "dmy_dash_month", None),

    # 3) MM/DD/YYYY HH:MM[:SS] [AM/PM]   (Walmart equip label va ko'plab boshqalar)
    (re.compile(
        r'(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})'
        r'(?P<sep>[ ,]+)'
        r'(?P<time>\d{1,2}:\d{2}(:\d{2})?)'
        r'(?P<ampm>\s*[AaPp]\.?[Mm]\.?)?'
    ), "mdy_slash", None),

    # 4) Month DD, YYYY HH:MM[:SS] [AM/PM]   masalan "July 13, 2026 1:34:45 PM"
    (re.compile(
        r'(?P<date>(' + '|'.join(_MONTHS_LONG + _MONTHS_SHORT) + r')\.?\s+\d{1,2},?\s+\d{4})'
        r'(?P<sep>[ ,]+)'
        r'(?P<time>\d{1,2}:\d{2}(:\d{2})?)'
        r'(?P<ampm>\s*[AaPp]\.?[Mm]\.?)?'
    ), "month_name", None),

    # 5) Sana va vaqt orasida boshqa matn bo'lgan holat:
    #    "Date: July 13, 2026   Time: 1:34:45 PM" -> sana va vaqtni ALOHIDA topamiz
    (re.compile(
        r'(?P<date>(' + '|'.join(_MONTHS_LONG + _MONTHS_SHORT) + r')\.?\s+\d{1,2},?\s+\d{4})'
    ), "month_name_date_only", None),

    (re.compile(
        r'(?P<date>\d{1,2}-(' + '|'.join(_MONTHS_SHORT) + r')-\d{2,4})'
    ), "dmy_dash_month_date_only", None),

    (re.compile(
        r'(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})(?![ ,]*\d{1,2}:\d{2})'
    ), "mdy_slash_date_only", None),

    # 6) Faqat vaqt, ajratuvchili: HH:MM[:SS] [AM/PM]
    (re.compile(
        r'(?<!\d)(?P<time>\d{1,2}:\d{2}(:\d{2})?)(?P<ampm>\s*[AaPp]\.?[Mm]\.?)?(?!\d)'
    ), "time_only", None),

    # 7) Faqat vaqt, AJRATUVCHISIZ 4 xonali HHMM (masalan Walmart "Time In: 1345")
    #    24-soat formati. XAVFSIZLIK: faqat "Time" so'ziga yaqin joyda qidiramiz,
    #    aks holda yil ("2026") yoki boshqa 4 xonali raqamlar bilan adashib qoladi.
    (re.compile(
        r'(?:[Tt]ime\s*(?:[Ii]n|[Oo]ut)?\s*[:\-]?\s*)'
        r'(?P<time>([01]\d|2[0-3])[0-5]\d)(?!\d)'
    ), "time_hhmm_compact", None),
]


def find_datetime_candidates(text: str) -> List[TimeMatch]:
    """Berilgan matndan barcha sana/vaqt nomzodlarini topadi (overlapsiz, pattern ustuvorligi bo'yicha)."""
    found: List[TimeMatch] = []
    occupied = [False] * len(text)

    for pattern, date_style, _ in _PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.start(), m.end()
            if any(occupied[s:e]):
                continue  # boshqa pattern allaqachon shu joyni egallagan

            gd = m.groupdict()
            date_text = gd.get("date")
            time_text = gd.get("time")
            ampm_raw = gd.get("ampm")

            has_date = date_text is not None
            has_time = time_text is not None
            has_ampm = bool(ampm_raw and ampm_raw.strip())
            has_seconds = bool(time_text and time_text.count(":") == 2)

            month_style = None
            if date_style in ("month_name", "month_name_date_only") and date_text:
                first_word = re.split(r'\s+', date_text.strip())[0].rstrip('.')
                month_style = "long" if first_word in _MONTHS_LONG else "short"
            elif date_style in ("dmy_dash_month", "dmy_dash_month_date_only"):
                month_style = "short"  # "20-Jun-26" uslubida oy doim qisqa yoziladi

            sep_date = "/"
            if has_date and date_style in ("mdy_slash", "mdy_slash_date_only") and date_text:
                sep_date = "-" if "-" in date_text else "/"
            elif date_style == "iso":
                sep_date = "-"
            elif date_style in ("dmy_dash_month", "dmy_dash_month_date_only"):
                sep_date = "-"

            upper_ampm = bool(ampm_raw and ampm_raw.strip()[0].isupper())

            # time_hhmm_compact uchun pattern "Time In:" kabi kontekstni ham
            # ushlab oladi, lekin PDF ichida faqat raqamning o'zini (masalan
            # "1345") almashtirishimiz kerak - shuning uchun bu holatda
            # raw_text/start/end ni 'time' group'ining chegaralaridan olamiz.
            if date_style == "time_hhmm_compact":
                t_start, t_end = m.span("time")
                out_raw_text = text[t_start:t_end]
                out_start, out_end = t_start, t_end
            else:
                out_raw_text = m.group(0)
                out_start, out_end = s, e

            found.append(TimeMatch(
                raw_text=out_raw_text,
                start=out_start, end=out_end,
                has_date=has_date,
                has_time=has_time,
                has_seconds=has_seconds,
                has_ampm=has_ampm,
                date_style=date_style,
                month_style=month_style,
                sep_date=sep_date,
                sep_time=":",
                upper_ampm=upper_ampm,
            ))
            for i in range(s, e):
                occupied[i] = True

    found.sort(key=lambda t: t.start)
    return found


# --------------------------------------------------------------------------
# 2) Foydalanuvchi kiritgan erkin matnni (masalan "July 13, 1:34:45 PM"
#    yoki "07/13/2026 13:45") datetime obyektiga aylantirish
# --------------------------------------------------------------------------

# Foydalanuvchi yil ko'rsatmasa, BOL ichidagi asl yildan foydalanamiz (year=None bo'lishi mumkin)
_USER_INPUT_PATTERNS = [
    # "July 13, 2026 1:34:45 PM" yoki "July 13 1:34:45 PM" (yilsiz)
    re.compile(
        r'(?P<month>' + '|'.join(_MONTHS_LONG + _MONTHS_SHORT) + r')\.?\s+'
        r'(?P<day>\d{1,2}),?\s*'
        r'(?P<year>\d{4})?,?\s*'
        r'(?P<hour>\d{1,2}):(?P<minute>\d{2})(:(?P<second>\d{2}))?'
        r'\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?',
        re.IGNORECASE
    ),
    # "07/13/2026 13:45:30" yoki "07/13 13:45"
    re.compile(
        r'(?P<month_num>\d{1,2})[/\-](?P<day>\d{1,2})(?:[/\-](?P<year>\d{2,4}))?'
        r'[ ,]+'
        r'(?P<hour>\d{1,2}):(?P<minute>\d{2})(:(?P<second>\d{2}))?'
        r'\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?'
    ),
]


def parse_user_input(text: str, fallback_year: Optional[int] = None) -> Optional[datetime]:
    """
    Foydalanuvchi kiritgan erkin formatdagi sana-vaqtni datetime ga aylantiradi.
    Agar yil kiritilmagan bo'lsa, fallback_year (BOL ichidagi asl yil) ishlatiladi.
    """
    text = text.strip()
    for pat in _USER_INPUT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        gd = m.groupdict()

        if "month" in gd and gd.get("month"):
            month = _MONTH_TO_NUM.get(gd["month"].lower())
        elif gd.get("month_num"):
            month = int(gd["month_num"])
        else:
            continue

        day = int(gd["day"])
        year = int(gd["year"]) if gd.get("year") else fallback_year
        if year is None:
            return None
        if year < 100:
            year += 2000

        hour = int(gd["hour"])
        minute = int(gd["minute"])
        second = int(gd["second"]) if gd.get("second") else 0

        ampm = (gd.get("ampm") or "").lower().replace(".", "")
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# 3) Yangi datetime'ni TimeMatch'da aniqlangan ASL formatga moslab matnga aylantirish
# --------------------------------------------------------------------------

def format_like_original(dt: datetime, tm: TimeMatch) -> str:
    """
    dt (yangi vaqt) ni tm (eski matn shabloni) bilan bir xil ko'rinishda
    matnga aylantiradi. Masalan, agar asli 24-soatlik va sekundsiz bo'lsa,
    natija ham shunday bo'ladi.
    """
    def build_time_str() -> str:
        if tm.date_style == "time_hhmm_compact":
            # ajratuvchisiz 4 xonali 24-soat format, masalan "1345"
            return f"{dt.hour:02d}{dt.minute:02d}"
        if tm.has_ampm:
            hour12 = dt.hour % 12
            if hour12 == 0:
                hour12 = 12
            s = f"{hour12}:{dt.minute:02d}"
            if tm.has_seconds:
                s += f":{dt.second:02d}"
            ampm_str = "PM" if dt.hour >= 12 else "AM"
            if not tm.upper_ampm:
                ampm_str = ampm_str.lower()
            s += f" {ampm_str}"
        else:
            # 24-soatlik (masalan Amazon formatidagi kabi) - AM/PM yozilmaydi
            s = f"{dt.hour:02d}:{dt.minute:02d}"
            if tm.has_seconds:
                s += f":{dt.second:02d}"
        return s

    def build_date_str() -> str:
        if tm.date_style in ("mdy_slash", "mdy_slash_date_only"):
            sep = tm.sep_date
            # asl matnda yil 2 xonali yoki 4 xonali ekanini aniqlaymiz
            parts = re.split(r'[/\-]', tm.raw_text.split()[0]) if tm.raw_text else []
            year_part = str(dt.year)
            if len(parts) == 3 and len(parts[2]) == 2:
                year_part = str(dt.year)[-2:]
            return f"{dt.month:02d}{sep}{dt.day:02d}{sep}{year_part}"
        elif tm.date_style == "iso":
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        elif tm.date_style in ("dmy_dash_month", "dmy_dash_month_date_only"):
            # masalan "20-Jun-26" - kun-OyQisqa-2xonaliYil
            month_name = _MONTHS_SHORT[dt.month - 1]
            # asl matnda yil necha xonali ekanini aniqlaymiz
            parts = tm.raw_text.split()[0].split("-") if tm.raw_text else []
            year_part = str(dt.year)
            if len(parts) == 3 and len(parts[2]) == 2:
                year_part = str(dt.year)[-2:]
            return f"{dt.day}-{month_name}-{year_part}"
        elif tm.date_style in ("month_name", "month_name_date_only"):
            month_name = _MONTHS_LONG[dt.month - 1] if tm.month_style == "long" else _MONTHS_SHORT[dt.month - 1]
            has_comma = "," in tm.raw_text
            if has_comma:
                return f"{month_name} {dt.day}, {dt.year}"
            else:
                return f"{month_name} {dt.day} {dt.year}"
        else:
            return dt.strftime("%m/%d/%Y")

    if tm.date_style in ("mdy_slash_date_only", "month_name_date_only", "dmy_dash_month_date_only"):
        # faqat sana qismi topilgan, vaqt boshqa joyda - faqat sanani qaytaramiz
        return build_date_str()

    if tm.date_style in ("time_only", "time_hhmm_compact") or not tm.has_date:
        # faqat vaqt qismi topilgan
        return build_time_str()

    # sana + vaqt bitta matnda
    return f"{build_date_str()} {build_time_str()}"


def looks_like_amazon(full_text: str) -> bool:
    """
    Amazon BOL'larini taniydigan oddiy heuristика:
    - matnda "Amazon" so'zi bor
    - va vaqtlar odatda AM/PM'siz, 24-soatlik formatda yoziladi
    """
    return bool(re.search(r'\bamazon\b', full_text, re.IGNORECASE))
