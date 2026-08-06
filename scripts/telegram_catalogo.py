#!/usr/bin/env python3
"""Importa capas do Telegram para o catalogo Cinemay.

Fluxo idempotente e conservador: nunca remove registros de fotos.json nem
arquivos de imagens. Processa no maximo MAX_IMAGES_PER_RUN fotos por execucao.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "imagens"
CATALOG_FILE = ROOT / "fotos.json"
REVIEW_FILE = ROOT / "revisao.json"
STATE_FILE = ROOT / "telegram_state.json"
MAX_IMAGES = int(os.getenv("MAX_IMAGES_PER_RUN", "20"))
MIN_TITLE_SCORE = int(os.getenv("MIN_TITLE_SCORE", "4"))
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = str(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
NOTIFY_CHAT_ID = str(os.getenv("TELEGRAM_NOTIFY_CHAT_ID", ALLOWED_CHAT_ID))
API = f"https://api.telegram.org/bot{TOKEN}"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def telegram(method: str, *, data=None, timeout=45):
    response = requests.post(f"{API}/{method}", data=data or {}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", f"Telegram: {method} falhou"))
    return payload["result"]


def notify(text: str) -> None:
    # Telegram limita mensagens a 4096 caracteres.
    for start in range(0, len(text), 3900):
        telegram("sendMessage", data={"chat_id": NOTIFY_CHAT_ID, "text": text[start:start + 3900]})


def normalize_title(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"[^0-9A-Za-zÀ-ÿ&'’:\- ]+", " ", line)
        line = re.sub(r"\s+", " ", line).strip(" -_:|")
        if 3 <= len(line) <= 80 and any(ch.isalpha() for ch in line):
            lines.append(line)
    if not lines:
        return ""
    # Capas normalmente exibem o titulo em uma das linhas mais longas.
    candidates = sorted(lines, key=lambda s: (len(s.split()) <= 8, len(s)), reverse=True)
    return candidates[0][:80]


def title_score(title: str) -> int:
    if not title:
        return 0
    words = title.split()
    letters = sum(ch.isalpha() for ch in title)
    score = min(letters // 4, 6)
    if 1 <= len(words) <= 8:
        score += 2
    if title.isupper() or title.istitle():
        score += 1
    return score


def ocr_title(content: bytes) -> str:
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image = ImageOps.exif_transpose(image)
    width, height = image.size
    if width < 1200:
        scale = 1200 / max(width, 1)
        image = image.resize((int(width * scale), int(height * scale)))
    gray = ImageOps.grayscale(image)
    gray = ImageEnhance.Contrast(gray).enhance(1.8).filter(ImageFilter.SHARPEN)
    # Tenta imagem inteira e faixas comuns de titulo; escolhe o melhor resultado.
    regions = [gray, gray.crop((0, 0, gray.width, int(gray.height * .45))),
               gray.crop((0, int(gray.height * .45), gray.width, gray.height))]
    titles = []
    for region in regions:
        text = pytesseract.image_to_string(region, lang="por+eng", config="--psm 6")
        title = normalize_title(text)
        titles.append(title)
    return max(titles, key=title_score, default="")


def slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return normalized[:50] or "capa"


def download_photo(file_id: str) -> bytes:
    info = telegram("getFile", data={"file_id": file_id})
    response = requests.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60
    )
    response.raise_for_status()
    return response.content


def existing_keys(catalog, review):
    return {item.get("telegram_file_unique_id") for item in catalog + review if item.get("telegram_file_unique_id")}


def main() -> int:
    IMAGES_DIR.mkdir(exist_ok=True)
    catalog = load_json(CATALOG_FILE, [])
    review = load_json(REVIEW_FILE, [])
    state = load_json(STATE_FILE, {"update_offset": 0})
    if not isinstance(catalog, list) or not isinstance(review, list):
        raise ValueError("fotos.json e revisao.json precisam conter listas JSON")

    updates = telegram("getUpdates", data={
        "offset": int(state.get("update_offset", 0)), "limit": 100, "timeout": 0,
        "allowed_updates": json.dumps(["message", "channel_post"]),
    })
    authorized = []
    ignored_unauthorized = 0
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id == ALLOWED_CHAT_ID:
            authorized.append((update, message))
        else:
            ignored_unauthorized += 1

    known = existing_keys(catalog, review)
    published, pending, duplicates, errors = [], [], [], []
    processed = 0
    last_consumed = int(state.get("update_offset", 0)) - 1

    for update, message in authorized:
        if processed >= MAX_IMAGES:
            break
        last_consumed = max(last_consumed, int(update["update_id"]))
        photos = message.get("photo") or []
        document = message.get("document") or {}
        is_image_doc = str(document.get("mime_type", "")).startswith("image/")
        if not photos and not is_image_doc:
            continue
        chosen = photos[-1] if photos else document
        unique_id = chosen.get("file_unique_id")
        if unique_id in known:
            duplicates.append(unique_id)
            continue

        processed += 1
        try:
            content = download_photo(chosen["file_id"])
            digest = hashlib.sha256(content).hexdigest()[:12]
            caption = (message.get("caption") or "").strip()
            title = normalize_title(caption) or ocr_title(content)
            confident = bool(normalize_title(caption)) or title_score(title) >= MIN_TITLE_SCORE
            extension = ".jpg"
            filename = f"{slug(title)}-{digest}{extension}"
            rel_url = f"imagens/{filename}"
            (IMAGES_DIR / filename).write_bytes(content)
            item = {
                "url": rel_url,
                "legenda": title if confident else "",
                "telegram_file_unique_id": unique_id,
                "recebido_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if confident:
                catalog.append(item)
                published.append(f"{title} ({filename})")
            else:
                item["sugestao_ocr"] = title
                review.append(item)
                pending.append(filename)
            known.add(unique_id)
        except Exception as exc:  # continua as outras capas do lote
            errors.append(f"update {update['update_id']}: {type(exc).__name__}: {exc}")

    if last_consumed >= int(state.get("update_offset", 0)):
        state["update_offset"] = last_consumed + 1
    state["ultima_execucao"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(CATALOG_FILE, catalog)
    save_json(REVIEW_FILE, review)
    save_json(STATE_FILE, state)

    lines = ["🎬 Cinemay — processamento concluído", ""]
    lines.append(f"✅ Publicadas: {len(published)}")
    lines.extend(f"• {name}" for name in published)
    lines.append(f"⚠️ Aguardando revisão: {len(pending)}")
    lines.extend(f"• {name}" for name in pending)
    lines.append(f"↩️ Repetidas ignoradas: {len(duplicates)}")
    if errors:
        lines.append(f"❌ Erros: {len(errors)}")
        lines.extend(f"• {error}" for error in errors)
    if ignored_unauthorized:
        lines.append(f"🔒 Mensagens não autorizadas ignoradas: {ignored_unauthorized}")
    if pending:
        lines += ["", "Abra revisao.json, preencha apenas a legenda e salve. O fluxo de revisão publicará as capas."]
    if processed or errors:
        notify("\n".join(lines))

    summary = {"published": len(published), "pending": len(pending), "errors": len(errors)}
    print(json.dumps(summary))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            notify(f"❌ Cinemay — falha geral\n{type(exc).__name__}: {exc}")
        except Exception:
            pass
        print(f"ERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

