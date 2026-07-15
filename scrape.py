"""Разбор страницы объявления ТС (telegra.ph / teletype.in): текст и фото.

На этих страницах пробег и повреждения обычно НЕ в тексте, а на фотографиях
(одометр, кузов) — поэтому главная ценность здесь фото, которые дальше уходят
в vision-модель для чтения пробега и оценки состояния."""
import html
import re

import requests

_UA = {"User-Agent": "Mozilla/5.0 (compatible; stok-analiz/1.0)"}
_IMG_RE = re.compile(r'src="(https?://[^"]+?\.(?:jpe?g|png|webp)[^"]*)"', re.I)
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

    # картинки — абсолютные URL, без дублей, с сохранением порядка
    images, seen = [], set()
    for m in _IMG_RE.finditer(page):
        u = html.unescape(m.group(1))
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
