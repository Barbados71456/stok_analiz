"""Веб-приложение: дашборд стока, фильтры по географии, детальный анализ,
лента изменений, ручное/фоновое обновление и оценка."""
import threading
import time
from datetime import datetime

from flask import (Flask, abort, redirect, render_template, request, url_for, flash)

import config
import db
import ingest
import scoring
import valuation

app = Flask(__name__)
app.secret_key = "auto-stock-local"  # только для flash-сообщений


@app.context_processor
def inject_globals():
    # storage_cost используется в футере base.html на всех страницах
    return {"storage_cost": config.STORAGE_COST_PER_DAY}

# ---------- вспомогательные ----------

def _check_admin():
    """Простейшая защита изменяющих действий, если задан ADMIN_TOKEN."""
    if not config.ADMIN_TOKEN:
        return True
    return request.args.get("token") == config.ADMIN_TOKEN or \
        request.form.get("token") == config.ADMIN_TOKEN


def enrich(row, vals_by_vin=None):
    """Дополняет карточку оценкой и аналитикой. vals_by_vin — предзагруженный
    словарь оценок {vin: val}, чтобы не делать запрос на каждую строку (N+1)."""
    row = dict(row)
    if vals_by_vin is not None:
        val = vals_by_vin.get(row["vin"])
    else:
        val = db.query_one("SELECT * FROM valuations WHERE vin=?", (row["vin"],))
    row["val"] = val
    row["buyer"] = scoring.buyer_analysis(row, val)
    row["seller"] = scoring.seller_analysis(row, val, config.STORAGE_COST_PER_DAY)
    return row


# ---------- маршруты ----------

@app.route("/")
def index():
    region = request.args.get("region", "").strip()
    brand = request.args.get("brand", "").strip()
    verdict = request.args.get("verdict", "").strip()
    show = request.args.get("show", "active")  # active | removed | all
    sort = request.args.get("sort", "discount")

    where = []
    params = []
    if show == "active":
        where.append("status='active'")
    elif show == "removed":
        where.append("status='removed'")
    if region:
        where.append("region=?"); params.append(region)
    if brand:
        where.append("brand=?"); params.append(brand)
    sql = "SELECT * FROM listings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # оценки грузим одним запросом, а не по строке (важно для Postgres по сети)
    vals_by_vin = {v["vin"]: v for v in db.query("SELECT * FROM valuations")}
    rows = [enrich(r, vals_by_vin) for r in db.query(sql, tuple(params))]

    if verdict:
        rows = [r for r in rows if r["buyer"]["verdict"] == verdict]

    # сортировка
    def keyf(r):
        d = r["buyer"].get("discount_pct")
        return d if d is not None else -999
    if sort == "discount":
        rows.sort(key=keyf, reverse=True)
    elif sort == "price":
        rows.sort(key=lambda r: r.get("price") or 0, reverse=True)
    elif sort == "days":
        rows.sort(key=lambda r: r["buyer"].get("days_on_stock") or 0, reverse=True)

    regions = [r["region"] for r in db.query(
        "SELECT DISTINCT region FROM listings WHERE region IS NOT NULL ORDER BY region")]
    brands = [r["brand"] for r in db.query(
        "SELECT DISTINCT brand FROM listings WHERE brand IS NOT NULL ORDER BY brand")]

    stats = _dashboard_stats(rows)
    last_snap = db.query_one("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")

    return render_template("index.html", rows=rows, regions=regions, brands=brands,
                           region=region, brand=brand, verdict=verdict, show=show,
                           sort=sort, stats=stats, last_snap=last_snap,
                           storage_cost=config.STORAGE_COST_PER_DAY)


def _dashboard_stats(rows):
    valued = [r for r in rows if r.get("val")]
    buy = [r for r in rows if r["buyer"]["verdict"] in ("Сильно брать", "Брать")]
    banned = [r for r in rows if r.get("ban_court") or r.get("ban_fssp")]
    total_price = sum(r.get("price") or 0 for r in rows)
    return {
        "count": len(rows),
        "valued": len(valued),
        "buy_signals": len(buy),
        "banned": len(banned),
        "total_price": total_price,
    }


@app.route("/car/<vin>")
def car(vin):
    row = db.query_one("SELECT * FROM listings WHERE vin=?", (vin,))
    if not row:
        abort(404)
    row = enrich(row)
    row["comparables"] = scoring.parse_comparables(row.get("val"))
    history = db.query(
        "SELECT * FROM price_history WHERE vin=? ORDER BY observed_at", (vin,))
    changes = db.query(
        "SELECT * FROM changes WHERE vin=? ORDER BY id DESC", (vin,))
    return render_template("car.html", row=row, history=history, changes=changes,
                           storage_cost=config.STORAGE_COST_PER_DAY)


@app.route("/changes")
def changes():
    rows = db.query("SELECT * FROM changes ORDER BY id DESC LIMIT 300")
    return render_template("changes.html", rows=rows)


def _refresh_job():
    """Обновление стока + авто-оценка только ВНОВЬ появившихся машин. Крутится
    в фоне, чтобы первичная полная загрузка не упиралась в таймаут gunicorn."""
    try:
        summary = ingest.ingest()
        if config.AUTO_VALUATE and not summary.get("unchanged"):
            valuation.valuate_missing()
    except Exception as e:  # noqa: BLE001
        print(f"[refresh] ошибка: {e}", flush=True)


@app.route("/refresh", methods=["POST"])
def refresh():
    if not _check_admin():
        abort(403)
    threading.Thread(target=_refresh_job, daemon=True).start()
    flash("Запущено обновление из таблицы в фоне. Обновите страницу через минуту — "
          "новые и снятые машины появятся в истории изменений.")
    return redirect(request.referrer or url_for("index"))


@app.route("/valuate/<vin>", methods=["POST"])
def valuate_one(vin):
    if not _check_admin():
        abort(403)
    status, res = valuation.valuate_vin(vin, force=True)
    if status == "error":
        flash(f"Ошибка оценки: {res}")
    else:
        flash("Оценка обновлена." if status == "valued" else "Оценка уже актуальна.")
    return redirect(url_for("car", vin=vin))


@app.route("/valuate-all", methods=["POST"])
def valuate_all():
    if not _check_admin():
        abort(403)
    threading.Thread(target=valuation.valuate_missing, daemon=True).start()
    flash("Запущена оценка всех необсчитанных машин в фоне. Обновите страницу через минуту.")
    return redirect(request.referrer or url_for("index"))


@app.template_filter("money")
def money(v):
    if v is None:
        return "—"
    return f"{int(v):,}".replace(",", " ") + " ₽"


@app.template_filter("dt")
def dt(v):
    if not v:
        return "—"
    try:
        return datetime.fromisoformat(v).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return v


# ---------- фоновый планировщик ----------

def _scheduler_loop():
    interval = config.POLL_INTERVAL_HOURS * 3600
    while True:
        try:
            summary = ingest.ingest()
            if config.AUTO_VALUATE and not summary.get("unchanged"):
                valuation.valuate_missing()
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] ошибка: {e}", flush=True)
        time.sleep(interval)


def start_scheduler():
    db.init_db()
    # первичный забор при старте, если стока ещё нет
    if not db.query_one("SELECT 1 FROM listings LIMIT 1"):
        try:
            ingest.ingest()
        except Exception as e:  # noqa: BLE001
            print(f"[startup] ingest error: {e}", flush=True)
    if config.ENABLE_SCHEDULER:
        threading.Thread(target=_scheduler_loop, daemon=True).start()


# запускаем планировщик один раз (в т.ч. под gunicorn)
_started = False
_lock = threading.Lock()


@app.before_request
def _boot():
    global _started
    if not _started:
        with _lock:
            if not _started:
                start_scheduler()
                _started = True


if __name__ == "__main__":
    import os
    start_scheduler()
    _started = True
    port = int(os.environ.get("PORT", "5060"))
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
