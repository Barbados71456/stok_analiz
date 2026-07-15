"""Аналитика поверх рыночной оценки: вердикт покупателю и рекомендации продавцу.

Все функции — чистые и прозрачные (не зависят от Claude). Работают с рыночной
оценкой (market_mid и т.д.) и фактами из карточки: запреты, срок на стоянке, цена."""
import json
from datetime import date, datetime


def _rub(n):
    """Число → '600 000' (разряды через неразрывный пробел не нужны — обычный пробел)."""
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")


def days_on_stock(row):
    if not row.get("date_added"):
        return None
    try:
        d = datetime.fromisoformat(row["date_added"]).date()
    except (ValueError, TypeError):
        return None
    return (date.today() - d).days


def legal_risk(row):
    """Юридический риск по запретам. Запрет = машину нельзя перерегистрировать в ГИБДД,
    пока он не снят. Это ключевой фактор решения о покупке."""
    court = bool(row.get("ban_court"))
    fssp = bool(row.get("ban_fssp"))
    if court and fssp:
        return {"level": "critical", "label": "Запрет суда + ФССП",
                "note": "Двойное обременение. Перерегистрация невозможна до снятия обоих запретов. "
                        "Покупка только с юристом и большим дисконтом.",
                "penalty": 0.35}
    if court:
        return {"level": "high", "label": "Запрет суда",
                "note": "Есть судебный запрет на регистрационные действия. Снять сложнее, чем ФССП.",
                "penalty": 0.25}
    if fssp:
        return {"level": "high", "label": "Запрет ФССП",
                "note": "Запрет от пристава. Снимается после погашения долга/окончания производства.",
                "penalty": 0.20}
    return {"level": "none", "label": "Без запретов", "note": "Ограничений на регистрацию нет.",
            "penalty": 0.0}


def buyer_analysis(row, val):
    """Вердикт для покупателя. val — строка/словарь оценки или None."""
    risk = legal_risk(row)
    price = row.get("price")
    out = {
        "risk": risk,
        "days_on_stock": days_on_stock(row),
        "discount_pct": None,
        "verdict": "нет данных",
        "verdict_class": "muted",
        "reasons": [],
        "fair_buy_price": None,
    }
    if not val or not val.get("market_mid") or not price:
        out["reasons"].append("Нет рыночной оценки — запусти анализ.")
        return out

    mid = val["market_mid"]
    # дисконт объявления к рынку чистого аналога
    discount = (mid - price) / mid if mid else 0
    out["discount_pct"] = round(discount * 100, 1)

    # справедливая цена покупки с учётом юридического дисконта за риск
    fair = mid * (1 - risk["penalty"])
    out["fair_buy_price"] = int(fair)

    reasons = []
    if discount > 0:
        reasons.append(f"Цена ниже рынка на {out['discount_pct']}% (рынок ~{_rub(mid)} ₽).")
    else:
        reasons.append(f"Цена выше рынка на {abs(out['discount_pct'])}% (рынок ~{_rub(mid)} ₽).")

    # запас = насколько цена стока ниже справедливой цены покупки (с учётом риска)
    margin = (fair - price) / mid if mid else 0

    if risk["level"] == "critical":
        verdict, vclass = "Избегать / только с юристом", "danger"
        reasons.append(risk["note"])
    elif margin >= 0.20:
        verdict, vclass = "Сильно брать", "success"
        reasons.append("Даже с учётом юррисков цена оставляет хороший запас.")
    elif margin >= 0.08:
        verdict, vclass = "Брать", "success"
        reasons.append("Цена с учётом рисков привлекательна.")
    elif margin >= -0.05:
        verdict, vclass = "Держать / торговаться", "warn"
        reasons.append("Цена близка к справедливой с учётом рисков — есть смысл торговаться.")
    else:
        verdict, vclass = "Переоценено / мимо", "danger"
        reasons.append("С учётом рисков цена завышена.")

    if risk["level"] in ("high",) and vclass == "success":
        reasons.append("⚠️ " + risk["note"])

    # ликвидность как дополнительный сигнал
    dts = val.get("days_to_sell")
    if dts:
        if dts <= 30:
            reasons.append(f"Ликвидная модель (~{dts} дн. до продажи) — легко перепродать.")
        elif dts >= 90:
            reasons.append(f"Низкая ликвидность (~{dts} дн.) — сложнее выйти в деньги.")

    out["verdict"] = verdict
    out["verdict_class"] = vclass
    out["reasons"] = reasons
    return out


def seller_analysis(row, val, storage_cost_per_day):
    """Рекомендации владельцу: цена для быстрой продажи, срочность, расходы на хранение."""
    dos = days_on_stock(row)
    price = row.get("price")
    out = {
        "days_on_stock": dos,
        "storage_cost_accrued": None,   # уже потрачено на хранение
        "storage_cost_per_month": int(storage_cost_per_day * 30),
        "recommend_price_30d": None,    # цена для продажи за ~30 дней
        "recommend_price_60d": None,
        "urgency": "нет данных",
        "urgency_class": "muted",
        "recommendations": [],
    }
    if dos is not None:
        out["storage_cost_accrued"] = int(dos * storage_cost_per_day)

    recs = []
    if not val or not val.get("market_mid") or not price:
        recs.append("Нет рыночной оценки — запусти анализ, чтобы получить ценовые рекомендации.")
        out["recommendations"] = recs
        return out

    mid = val["market_mid"]
    low = val.get("market_low") or int(mid * 0.9)
    dts = val.get("days_to_sell") or 45

    # цена для быстрой продажи: у нижней границы рынка; за 30 дней — агрессивнее
    out["recommend_price_30d"] = int(min(price, low * 0.98))
    out["recommend_price_60d"] = int(min(price, (low + mid) / 2))

    # переоценка объявления относительно рынка тормозит продажу
    overpriced = price > mid
    if overpriced:
        recs.append(
            f"Цена {_rub(price)} ₽ выше рынка (~{_rub(mid)} ₽) — это главный тормоз. "
            f"Снизьте хотя бы до {_rub(out['recommend_price_60d'])} ₽."
        )

    # экономика простоя: сколько съест хранение за ожидаемый срок продажи
    if dos is not None:
        expected_more = max(dts - dos, 0)
        extra_storage = int(expected_more * storage_cost_per_day)
        recs.append(
            f"На стоянке уже {dos} дн. (потрачено ~{_rub(out['storage_cost_accrued'])} ₽ на хранение). "
            f"По рынку продаётся ~{dts} дн."
        )
        # решение «торопиться ли»: если хранение до продажи сопоставимо с возможной скидкой —
        # выгоднее снизить цену сейчас
        possible_cut = max(price - out["recommend_price_30d"], 0)

        if dos > dts:
            urgency, uclass = "Высокая — машина зависла", "danger"
            recs.append(
                "Машина стоит дольше среднего срока продажи. Каждый месяц простоя ≈ "
                f"{_rub(out['storage_cost_per_month'])} ₽ убытка. Снижайте цену решительно "
                f"(до {_rub(out['recommend_price_30d'])} ₽), торг здесь дешевле хранения."
            )
        elif extra_storage >= possible_cut and possible_cut > 0:
            urgency, uclass = "Средняя — выгоднее снизить цену", "warn"
            recs.append(
                f"Ожидаемое хранение до продажи (~{_rub(extra_storage)} ₽) сопоставимо со скидкой "
                f"(~{_rub(possible_cut)} ₽). Дешевле сбросить цену сейчас, чем платить за стоянку."
            )
        elif dos < 21 and not overpriced:
            urgency, uclass = "Низкая — не торопитесь", "success"
            recs.append("Машина в стоке недавно и оценена по рынку — можно подождать покупателя по хорошей цене.")
        else:
            urgency, uclass = "Средняя", "warn"
    else:
        urgency, uclass = "нет данных", "muted"

    out["urgency"] = urgency
    out["urgency_class"] = uclass
    out["recommendations"] = recs
    return out


def parse_comparables(val):
    if not val or not val.get("comparables"):
        return []
    try:
        return json.loads(val["comparables"])
    except (ValueError, TypeError):
        return []
