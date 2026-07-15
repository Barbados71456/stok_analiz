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

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=config.POLZA_BASE_URL, api_key=config.POLZA_AI_API_KEY)
    return _client


def input_hash(row):
    key = f"{row['brand']}|{row['year']}|{row['region']}|{row['price']}|{row['make_raw']}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


SYSTEM = (
    "Ты — эксперт по оценке подержанных автомобилей на российском вторичном рынке "
    "(Avito, Auto.ru, Дром). Тебе дают карточку машины из стока залогового/"
    "арестованного авто. Оцени справедливую рыночную стоимость ЧИСТОГО аналога "
    "(без юридических обременений) по своим знаниям о рынке: марка, модель, год, регион. "
    "Учитывай регион (Москва/СПб дороже, регионы дешевле) и возраст. Оцени ликвидность — "
    "за сколько дней такая машина в среднем продаётся.\n\n"
    "ВАЖНО: цена — для чистого аналога, БЕЗ учёта запретов (юридические риски учитываются "
    "отдельно в приложении, не занижай из-за них рыночную цену).\n\n"
    "Верни СТРОГО один JSON-объект без markdown и текста вокруг:\n"
    "{\n"
    '  "market_low": целое (руб),\n'
    '  "market_mid": целое (руб, наиболее вероятная цена),\n'
    '  "market_high": целое (руб),\n'
    '  "days_to_sell": целое (среднее число дней до продажи),\n'
    '  "reasoning": строка (2-4 предложения обоснования),\n'
    '  "comparables": [ {"title": строка, "price": целое, "source": строка} ]\n'
    "}"
)


def _extract_json(text):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("в ответе модели не найден JSON")
    return json.loads(m.group(0))


def valuate_row(row):
    """Один запрос к Polza.ai. Возвращает dict с оценкой."""
    if not config.POLZA_AI_API_KEY:
        raise RuntimeError("оценка не настроена: задайте POLZA_AI_API_KEY")
    prompt = (
        f"Оцени рыночную стоимость машины:\n"
        f"- Марка/модель: {row['make_raw']}\n"
        f"- Бренд: {row['brand']}\n"
        f"- Год выпуска: {row['year']}\n"
        f"- Регион: {row['region']}\n"
        f"- Цена продажи в стоке: {row['price']} руб.\n"
        f"Верни JSON по схеме."
    )
    resp = client().chat.completions.create(
        model=config.POLZA_MODEL,
        max_tokens=1500,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": prompt}],
    )
    text = resp.choices[0].message.content
    data = _extract_json(text)
    return {
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
            reasoning,comparables,model,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(vin) DO UPDATE SET
             input_hash=excluded.input_hash, market_low=excluded.market_low,
             market_mid=excluded.market_mid, market_high=excluded.market_high,
             days_to_sell=excluded.days_to_sell, reasoning=excluded.reasoning,
             comparables=excluded.comparables, model=excluded.model,
             created_at=excluded.created_at""",
        (vin, ih, v["market_low"], v["market_mid"], v["market_high"], v["days_to_sell"],
         v["reasoning"], v["comparables"], v["model"], datetime.utcnow().isoformat()),
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
    done, errors = 0, 0
    for row in rows:
        try:
            v = valuate_row(row)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        save_valuation(row["vin"], input_hash(row), v)
        done += 1
        if limit and done >= limit:
            break
    return {"valued": done, "errors": errors}
