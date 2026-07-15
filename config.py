"""Конфигурация приложения. Значения берутся из переменных окружения (Render Environment),
с разумными дефолтами для локального запуска."""
import os

# --- Источник данных ---
# ID гугл-таблицы со стоком машин. Экспортируем как CSV.
SHEET_ID = os.environ.get(
    "SHEET_ID", "1EctV3modZlvz8V0Zb4nVgN1t5N1950WCnwn9zs4ni9w"
)
# Номер листа (gid). 0 — первый лист.
SHEET_GID = os.environ.get("SHEET_GID", "0")
SHEET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
)

# --- Хранилище ---
# Приоритет: Postgres общей базы проектов Альфы (та же, что alfa_collection),
# в ОТДЕЛЬНОЙ схеме DB_SCHEMA. Если реквизиты не заданы — локальный SQLite.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.sqlite"))

# Реквизиты Postgres (совместимо с проектами Альфы): либо DATABASE_URL, либо DB_*.
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip() or None
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_HOST = (os.environ.get("DB_HOST") or "").strip() or None
DB_PORT = os.environ.get("DB_PORT", "5432")

# Частая ошибка: в DB_HOST кладут целую строку подключения postgresql://...
# Тогда трактуем её как DATABASE_URL, а DB_HOST обнуляем.
if DB_HOST and DB_HOST.startswith(("postgres://", "postgresql://")):
    DATABASE_URL = DB_HOST
    DB_HOST = None
# То же, если полный URL случайно попал в DB_NAME.
if not DATABASE_URL and DB_NAME and DB_NAME.startswith(("postgres://", "postgresql://")):
    DATABASE_URL = DB_NAME
    DB_NAME = None
# Отдельная схема этого приложения внутри общей базы Альфы.
DB_SCHEMA = os.environ.get("DB_SCHEMA", "stok_analiz")

USE_POSTGRES = bool(DATABASE_URL or (DB_HOST and DB_NAME and DB_USER))

# --- Оценка через Polza.ai (OpenAI-совместимый прокси к Claude, как в проектах Альфы) ---
POLZA_AI_API_KEY = os.environ.get("POLZA_AI_API_KEY")
POLZA_BASE_URL = os.environ.get("POLZA_BASE_URL", "https://polza.ai/api/v1")
POLZA_MODEL = os.environ.get("POLZA_MODEL", "anthropic/claude-sonnet-5")

# --- Экономика (для рекомендаций продавцу) ---
# Стоимость хранения одной машины в сутки, руб. Настраивается в UI и через env.
STORAGE_COST_PER_DAY = float(os.environ.get("STORAGE_COST_PER_DAY", "500"))

# --- Планировщик ---
# Как часто автоматически опрашивать таблицу и обновлять анализ, в часах.
POLL_INTERVAL_HOURS = float(os.environ.get("POLL_INTERVAL_HOURS", "1"))
# Автоматически оценивать машину при ПЕРВОМ появлении в стоке.
# Пересчёт уже оценённых машин делается только вручную (кнопкой).
AUTO_VALUATE = os.environ.get("AUTO_VALUATE", "1") == "1"
# Включать ли фоновый планировщик (на Render — да; при отладке можно 0).
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "1") == "1"

# Простой пароль для действий, меняющих данные (обновление/оценка). Пусто = без защиты.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
