"""Разбор страницы объявления ТС (telegra.ph / teletype.in): текст и фото.

На этих страницах пробег и повреждения обычно НЕ в тексте, а на фотографиях
(одометр, кузов) — поэтому главная ценность здесь фото, которые дальше уходят
в vision-модель для чтения пробега и оценки состояния."""
import base64
import html
import re
from urllib.parse import urljoin

import requests

_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36")}
# ловим и абсолютные, и относительные src (telegra.ph отдаёт /file/xxx.jpg)
_IMG_RE = re.compile(r'(?:src|data-src)="([^"]+?\.(?:jpe?g|png|webp)[^"]*)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def fetch_listing(url, max_images=8):
    """Возвращает {'text': str, 'images': [url], 'error': str|None}."""
    if not url:
        return {"text": "", "images": [], "error": "нет ссылки на ТС"}
    try:
        r = requests.get(url, timeout=25, headers=_UA)
        r.raise_for_status()
        page = r.text
    except Exception as e:  # noqa: BLE001
        return {"text": "", "images": [], "error": f"не удалось открыть ссылку: {e}"}

    # картинки: относительные приводим к абсолютным через базовый URL, без дублей
    images, seen = [], set()
    for m in _IMG_RE.finditer(page):
        u = urljoin(url, html.unescape(m.group(1)))
        if not u.startswith("http"):
            continue
        if u not in seen:
            seen.add(u)
            images.append(u)
        if len(images) >= max_images:
            break

    # текст страницы (иногда есть краткое описание/комплектация)
    text = html.unescape(_TAG_RE.sub(" ", page))
    text = _SPACE_RE.sub(" ", text).strip()
    # обрезаем служебную обвязку — берём разумный кусок
    text = text[:2000]

    return {"text": text, "images": images, "error": None}


_CT_BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp"}


def download_images_b64(urls, max_images=6, max_total_bytes=4_000_000,
                        max_one_bytes=2_500_000):
    """Скачивает фото САМ (с нашим UA) и возвращает data-URI (base64).

    Так модель не качает URL со стороннего хоста (Anthropic-фетчер их не достаёт
    и валит запрос 400) — байты отдаём инлайном. Пропускаем недоступные/огромные."""
    out, total = [], 0
    for u in urls:
        try:
            r = requests.get(u, timeout=20, headers=_UA)
            r.raise_for_status()
            data = r.content
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) > max_one_bytes:
            continue
        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ct.startswith("image/"):
            ext = u.rsplit(".", 1)[-1].split("?")[0].lower()
            ct = _CT_BY_EXT.get(ext, "image/jpeg")
        if total + len(data) > max_total_bytes:
            break
        total += len(data)
        out.append(f"data:{ct};base64,{base64.b64encode(data).decode()}")
        if len(out) >= max_images:
            break
    return out
