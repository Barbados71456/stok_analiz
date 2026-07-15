"""Забор данных из гугл-таблицы, нормализация и детект изменений."""
import csv
import hashlib
import io
import re
from datetime import datetime, date

import requests

import config
import db

# --- Нормализация регионов ---
# Ключ — очищенная строка (lower, без лишних пробелов), значение — канон.
REGION_CANON = {
    "санкт петербург": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "ростов на дону": "Ростов-на-Дону",
    "ростов-на-дону": "Ростов-на-Дону",
    "набережные челны": "Набережные Челны",
    "минеральные воды": "Минеральные Воды",
    "мск": "Москва",
    "нижний новогород": "Нижний Новгород",
    "нижний новгород": "Нижний Новгород",
}

KNOWN_BRANDS = [
    "TOYOTA", "HYUNDAI", "KIA", "NISSAN", "FORD", "RENAULT", "VOLKSWAGEN",
    "MERCEDES", "BMW", "AUDI", "PORSCHE", "MAZDA", "HONDA", "CHERY", "GEELY",
    "OMODA", "JAC", "KAIYI", "CHANGAN", "OPEL", "PEUGEOT", "CITROEN", "SKODA",
    "SSANGYONG", "DATSUN", "INFINITI", "LADA", "VAZ", "MOSKVICH", "MITSUBISHI",
    "SUZUKI", "SUBARU", "LEXUS", "VOLVO", "JEEP", "LAND", "MINI",
    "HAVAL", "LIVAN", "EXEED", "GAC", "FAW", "TANK", "BAIC", "DONGFENG",
]

# Написания-синонимы (опечатки/латиница вместо оригинала) → канон.
BRAND_ALIASES = {
    "PORSHE": "PORSCHE", "VOLKSVAGEN": "VOLKSWAGEN", "MERCEDES-BENZ": "MERCEDES",
    "НИВА": "LADA", "NIVA": "LADA", "CHANGAN": "CHANGAN",
}

# Кириллические омоглифы → латиница. В таблице марки написаны смешанными
# буквами-двойниками (напр. SКОDА, НУUNDАI, VОLКSWАGЕN), это ломает поиск бренда.
HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "І": "I", "Ѕ": "S",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
})

# Кириллица → латиница для распознавания бренда (в таблице встречается и то, и то).
CYR_BRAND_MAP = {
    "тойота": "TOYOTA", "хендэ": "HYUNDAI", "хендай": "HYUNDAI", "хёндай": "HYUNDAI",
    "хундай": "HYUNDAI", "киа": "KIA", "ниссан": "NISSAN", "форд": "FORD",
    "рено": "RENAULT", "фольксваген": "VOLKSWAGEN", "фольцваген": "VOLKSWAGEN",
    "мерседес": "MERCEDES", "бмв": "BMW", "ауди": "AUDI", "порше": "PORSCHE",
    "мазда": "MAZDA", "хонда": "HONDA", "чери": "CHERY", "джили": "GEELY",
    "опель": "OPEL", "пежо": "PEUGEOT", "ситроен": "CITROEN", "шкода": "SKODA",
    "датсун": "DATSUN", "инфинити": "INFINITI", "лада": "LADA", "ваз": "VAZ",
    "москвич": "MOSKVICH", "мицубиси": "MITSUBISHI", "митсубиси": "MITSUBISHI",
    "чанган": "CHANGAN", "снанган": "CHANGAN", "нива": "LADA", "хавал": "HAVAL",
    "ливан": "LIVAN", "эксид": "EXEED", "танк": "TANK",
}


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_region(raw):
    key = _clean(raw).lower()
    if not key:
        return "—"
    if key in REGION_CANON:
        return REGION_CANON[key]
    return key.title()


def extract_brand(make_raw):
    txt = _clean(make_raw)
    # 1) чисто русские написания
    low = txt.lower()
    for cyr, lat in CYR_BRAND_MAP.items():
        if cyr in low:
            return lat
    # 2) сворачиваем кириллические буквы-двойники в латиницу и ищем бренд
    folded = txt.upper().translate(HOMOGLYPHS)
    for alias, canon in BRAND_ALIASES.items():
        if alias in folded:
            return canon
    for b in KNOWN_BRANDS:
        if b in folded:
            return "MERCEDES-BENZ" if b == "MERCEDES" else b
    # 3) запасной вариант — первое слово (уже свёрнутое)
    first = re.split(r"[\s,\-]", folded, 1)[0]
    return first or "—"


def parse_price(raw):
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def parse_year(raw):
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return None, "suspicious"
    y = int(digits)
    cur = date.today().year
    if 1980 <= y <= cur + 1:
        return y, "ok"
    # частая опечатка: 2913 → 2013
    if 2900 <= y <= 3100:
        y2 = y - 900
        if 1980 <= y2 <= cur + 1:
            return y2, "suspicious"
    return None, "suspicious"


def parse_date_added(raw):
    raw = _clean(raw)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _yesno(raw):
    return 1 if _clean(raw).lower().startswith("да") else 0


def fetch_rows():
    """Скачивает CSV и возвращает список нормализованных словарей по машинам."""
    resp = requests.get(config.SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], ""
    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    header = rows[0]
    out = []
    for r in rows[1:]:
        # выравниваем длину строки под заголовок
        r = (r + [""] * len(header))[: len(header)]
        vin = _clean(r[2]).upper()
        if not vin:
            continue  # без VIN не идентифицируем машину
        year, yflag = parse_year(r[3])
        out.append({
            "vin": vin,
            "date_added": parse_date_added(r[0]),
            "make_raw": _clean(r[1]),
            "brand": extract_brand(r[1]),
            "year": year,
            "year_flag": yflag,
            "region_raw": _clean(r[4]),
            "region": normalize_region(r[4]),
            "address": _clean(r[5]),
            "price": parse_price(r[6]),
            "url": _clean(r[7]),
            "ban_court": _yesno(r[8]),
            "ban_fssp": _yesno(r[9]),
        })
    return out, raw_hash


def _title(row):
    parts = [row.get("make_raw") or row.get("brand") or "?"]
    if row.get("year"):
        parts.append(str(row["year"]))
    if row.get("region"):
        parts.append(row["region"])
    return " · ".join(parts)


def ingest():
    """Главная точка: забирает таблицу, обновляет БД, фиксирует изменения.
    Возвращает сводку по изменениям."""
    db.init_db()
    rows, raw_hash = fetch_rows()
    now = datetime.utcnow().isoformat()

    # пропускаем, если контент не менялся с прошлого раза
    last = db.query_one("SELECT raw_hash FROM snapshots ORDER BY id DESC LIMIT 1")
    unchanged = bool(last and last["raw_hash"] == raw_hash)

    summary = {"added": 0, "removed": 0, "price_up": 0, "price_down": 0,
               "readded": 0, "unchanged": unchanged, "total": len(rows)}
    if unchanged:
        # фиксируем сам факт опроса, но строки не трогаем
        db.execute(
            "INSERT INTO snapshots (fetched_at, row_count, raw_hash) VALUES (?,?,?)",
            (now, len(rows), raw_hash),
        )
        return summary

    existing = {r["vin"]: r for r in db.query("SELECT * FROM listings")}
    seen = set()
    ops = []  # все записи копим и пишем одной транзакцией (атомарно + быстро по сети)

    ins_listing = (
        "INSERT INTO listings "
        "(vin,date_added,make_raw,brand,year,year_flag,region_raw,region,"
        "address,price,url,ban_court,ban_fssp,first_seen,last_seen,status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')")
    ins_price = "INSERT INTO price_history (vin,price,observed_at) VALUES (?,?,?)"
    ins_change = ("INSERT INTO changes (vin,ctype,old_value,new_value,title,created_at) "
                  "VALUES (?,?,?,?,?,?)")

    for row in rows:
        vin = row["vin"]
        seen.add(vin)
        prev = existing.get(vin)
        title = _title(row)

        if prev is None:
            ops.append((ins_listing,
                        (vin, row["date_added"], row["make_raw"], row["brand"], row["year"],
                         row["year_flag"], row["region_raw"], row["region"], row["address"],
                         row["price"], row["url"], row["ban_court"], row["ban_fssp"], now, now)))
            ops.append((ins_price, (vin, row["price"], now)))
            ops.append((ins_change, (vin, "added", None, str(row["price"]), title, now)))
            summary["added"] += 1
            continue

        # машина уже была: обновляем поля, ловим изменение цены и возврат из архива
        was_removed = prev["status"] == "removed"
        old_price = prev["price"]
        new_price = row["price"]

        ops.append((
            "UPDATE listings SET date_added=?,make_raw=?,brand=?,year=?,year_flag=?,"
            "region_raw=?,region=?,address=?,price=?,url=?,ban_court=?,ban_fssp=?,"
            "last_seen=?,status='active',removed_at=NULL WHERE vin=?",
            (row["date_added"], row["make_raw"], row["brand"], row["year"],
             row["year_flag"], row["region_raw"], row["region"], row["address"],
             new_price, row["url"], row["ban_court"], row["ban_fssp"], now, vin)))

        if was_removed:
            ops.append((ins_change, (vin, "readded", None, str(new_price), title, now)))
            summary["readded"] += 1

        if old_price != new_price and new_price is not None:
            ops.append((ins_price, (vin, new_price, now)))
            ctype = "price_down" if (old_price or 0) > new_price else "price_up"
            ops.append((ins_change, (vin, ctype, str(old_price), str(new_price), title, now)))
            summary[ctype] += 1

    # то, что пропало из выгрузки, помечаем как проданное/снятое
    for vin, prev in existing.items():
        if vin not in seen and prev["status"] == "active":
            ops.append(("UPDATE listings SET status='removed', removed_at=? WHERE vin=?",
                        (now, vin)))
            ops.append((ins_change, (vin, "removed", str(prev["price"]), None, _title(prev), now)))
            summary["removed"] += 1

    # снапшот с хешем — последней операцией той же транзакции: если что-то упадёт,
    # откатится всё и хеш не запишется, значит следующий заход повторит обработку
    ops.append(("INSERT INTO snapshots (fetched_at, row_count, raw_hash) VALUES (?,?,?)",
                (now, len(rows), raw_hash)))

    db.execute_many(ops)
    return summary
