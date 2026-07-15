"""Оценка рыночной стоимости машины через Polza.ai (OpenAI-совместимый прокси
к Claude, как в других проектах Альфы).

Модель оценивает справедливую рыночную цену чистого аналога по своим знаниям
о российском вторичном рынке (без live-поиска — Polza это чат-прокси). Результат
кешируется в БД и по умолчанию считается ОДИН раз — при первом появлении машины
в стоке. Пересчёт — только по команде пользователя (кнопка)."""
import hashlib
import json
import re
from datetime import datetime

import config
import db
import scrape

_client = None

# Итог последнего прогона оценки — чтобы ошибки фона были видны в UI/через /status.
LAST_RUN = {"valued": 0, "errors": 0, "last_error": None, "at": None}

# Живой прогресс текущего прогона оценки (для индикатора на дашборде).
PROGRESS = {"running": False, "total": 0, "done": 0, "errors": 0,
            "current": None, "current_vin": None, "last_error": None,
            "started_at": None, "finished_at": None}


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=config.POLZA_BASE_URL, api_key=config.POLZA_AI_API_KEY)
    return _client


def input_hash(row):
    key = f"{row['brand']}|{row['year']}|{row['region']}|{row['price']}|{row['make_raw']}|{row.get('url')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


SYSTEM = (
    "Ты — эксперт по оценке подержанных автомобилей на российском вторичном рынке "
    "(Avito, Auto.ru, Дром). Тебе дают карточку машины из стока залогового/"
    "арестованного авто И ФОТОГРАФИИ этой машины со страницы объявления.\n\n"
    "ОБЯЗАТЕЛЬНО по фотографиям определи:\n"
    "1) ПРОБЕГ — найди фото приборной панели/одометра и считай пробег в км. Если одометра "
    "на фото нет — оцени по состоянию и укажи это; поле mileage_km тогда приблизительное.\n"
    "2) СОСТОЯНИЕ и ПОВРЕЖДЕНИЯ — вмятины, царапины, коррозия, сколы, состояние салона, "
    "следы ДТП. Кратко опиши в поле condition.\n\n"
    "Затем оцени справедливую рыночную стоимость аналога БЕЗ юридических обременений, "
    "с поправкой на реальный пробег и состояние по фото. Учитывай регион (Москва/СПб дороже) "
    "и возраст. Оцени ликвидность — за сколько дней продаётся такая машина.\n\n"
    "ВАЖНО: юридические запреты (суд/ФССП) в рыночную цену НЕ закладывай — они учитываются "
    "отдельно в приложении.\n\n"
    "Верни СТРОГО один JSON-объект без markdown и текста вокруг:\n"
    "{\n"
    '  "mileage_km": целое или null (пробег по фото одометра, км),\n'
    '  "condition": строка (состояние и повреждения по фото, 1-3 предложения),\n'
    '  "market_low": целое (руб),\n'
    '  "market_mid": целое (руб, наиболее вероятная цена с учётом пробега и состояния),\n'
    '  "market_high": целое (руб),\n'
    '  "days_to_sell": целое (среднее число дней до продажи),\n'
    '  "reasoning": строка (2-4 предложения: как пробег и состояние повлияли на оценку),\n'
    '  "comparables": [ {"title": строка, "price": целое, "source": строка, "url": строка или null} ]\n'
    "}\n"
    "В comparables url указывай только если точно знаешь реальную ссылку на объявление; "
    "иначе ставь null (приложение само построит ссылку на поиск)."
)


def _extract_json(text):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("в ответе модели не найден JSON")
    return json.loads(m.group(0))


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def valuate_row(row):
    """Один запрос к Polza.ai (vision). Скрейпит ссылку ТС, шлёт фото модели,
    та читает пробег/повреждения и оценивает рынок с поправкой на них."""
    if not config.POLZA_AI_API_KEY:
        raise RuntimeError("оценка не настроена: задайте POLZA_AI_API_KEY")

    listing = scrape.fetch_listing(row.get("url"))
    # скачиваем фото сами и шлём как base64 — модель не тянет URL со стороннего хоста
    images = scrape.download_images_b64(listing["images"])

    prompt_text = (
        f"Оцени машину из стока:\n"
        f"- Марка/модель: {row['make_raw']}\n"
        f"- Бренд: {row['brand']}\n"
        f"- Год выпуска: {row['year']}\n"
        f"- Регион: {row['region']}\n"
        f"- Цена продажи в стоке: {row['price']} руб.\n"
    )
    if listing.get("text"):
        prompt_text += f"\nТекст со страницы объявления:\n{listing['text'][:1200]}\n"
    if images:
        prompt_text += (
            f"\nНиже {len(images)} фото машины со страницы объявления. Считай пробег с "
            f"одометра и оцени повреждения/состояние. Верни JSON по схеме."
        )
    else:
        prompt_text += (
            "\nФото со страницы получить не удалось — оцени по данным карточки, "
            "mileage_km = null, в condition укажи 'фото недоступны'. Верни JSON по схеме."
        )

    content = [{"type": "text", "text": prompt_text}]
    for u in images:
        content.append({"type": "image_url", "image_url": {"url": u}})

    resp = client().chat.completions.create(
        model=config.POLZA_MODEL,
        max_tokens=1500,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": content}],
    )
    text = resp.choices[0].message.content
    data = _extract_json(text)
    return {
        "mileage_km": _int_or_none(data.get("mileage_km")),
        "condition": str(data.get("condition", "")),
        "market_low": int(data["market_low"]),
        "market_mid": int(data["market_mid"]),
        "market_high": int(data["market_high"]),
        "days_to_sell": int(data.get("days_to_sell") or 0),
        "reasoning": str(data.get("reasoning", "")),
        "comparables": json.dumps(data.get("comparables", []), ensure_ascii=False),
        "model": config.POLZA_MODEL,
    }


def save_valuation(vin, ih, v):
    db.execute(
        """INSERT INTO valuations
           (vin,input_hash,market_low,market_mid,market_high,days_to_sell,
            mileage_km,condition,reasoning,comparables,model,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(vin) DO UPDATE SET
             input_hash=excluded.input_hash, market_low=excluded.market_low,
             market_mid=excluded.market_mid, market_high=excluded.market_high,
             days_to_sell=excluded.days_to_sell, mileage_km=excluded.mileage_km,
             condition=excluded.condition, reasoning=excluded.reasoning,
             comparables=excluded.comparables, model=excluded.model,
             created_at=excluded.created_at""",
        (vin, ih, v["market_low"], v["market_mid"], v["market_high"], v["days_to_sell"],
         v.get("mileage_km"), v.get("condition", ""), v["reasoning"], v["comparables"],
         v["model"], datetime.utcnow().isoformat()),
    )


def valuate_vin(vin, force=False):
    """Оценивает конкретную машину. Возвращает (status, valuation|error).
    status: 'cached' | 'valued' | 'error'.
    Без force повторно не считает, если оценка уже есть."""
    row = db.query_one("SELECT * FROM listings WHERE vin=?", (vin,))
    if not row:
        return "error", "машина не найдена"
    cur = db.query_one("SELECT * FROM valuations WHERE vin=?", (vin,))
    if cur and not force:
        return "cached", cur
    try:
        v = valuate_row(row)
    except Exception as e:  # noqa: BLE001 — показываем ошибку в UI, не роняем поток
        return "error", str(e)
    save_valuation(vin, input_hash(row), v)
    return "valued", db.query_one("SELECT * FROM valuations WHERE vin=?", (vin,))


def valuate_missing(limit=None):
    """Оценивает активные машины, у которых ЕЩЁ НЕТ оценки (первое появление в стоке).
    Уже оценённые не трогает — пересчёт только вручную. Для авто-оценки после ingest
    и кнопки 'оценить необсчитанные'."""
    rows = db.query(
        "SELECT l.* FROM listings l LEFT JOIN valuations v ON v.vin=l.vin "
        "WHERE l.status='active' AND v.vin IS NULL")
    done, errors, last_error = 0, 0, None
    PROGRESS.update(running=True, total=len(rows), done=0, errors=0,
                    current=None, current_vin=None,
                    started_at=datetime.utcnow().isoformat(), finished_at=None)
    try:
        for row in rows:
            title = " · ".join(str(x) for x in
                               [row.get("make_raw") or row.get("brand"),
                                row.get("year"), row.get("region")] if x)
            PROGRESS.update(current=title, current_vin=row["vin"])
            try:
                v = valuate_row(row)
            except Exception as e:  # noqa: BLE001
                errors += 1
                last_error = f"{row['vin']} ({title}): {type(e).__name__}: {e}"
                PROGRESS["errors"] = errors
                PROGRESS["last_error"] = last_error
                print(f"[valuate] ОШИБКА {last_error}", flush=True)  # видно в логах Render
                continue
            save_valuation(row["vin"], input_hash(row), v)
            done += 1
            PROGRESS["done"] = done
            if limit and done >= limit:
                break
    finally:
        PROGRESS.update(running=False, current=None, current_vin=None,
                        finished_at=datetime.utcnow().isoformat())
    LAST_RUN.update(valued=done, errors=errors, last_error=last_error,
                    at=datetime.utcnow().isoformat())
    return {"valued": done, "errors": errors, "last_error": last_error}
