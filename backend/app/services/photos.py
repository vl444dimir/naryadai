import hashlib
import io
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import HTTPException


def dhash(image):
    reduced = image.convert("L").resize((9, 8))
    pixels = list(reduced.get_flattened_data())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return f"{value:016x}"


def compress_photo(raw):
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(422, "Разрешены JPEG, PNG и WebP")
            if original.width * original.height > 25_000_000:
                raise HTTPException(413, "Слишком большое разрешение фотографии")
            original.load()
            image = ImageOps.exif_transpose(original).convert("RGB")
            fingerprint = dhash(image)
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            return output.getvalue(), hashlib.sha256(raw).hexdigest(), fingerprint
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Файл не является корректным изображением")


def resolve_photo_path(upload_dir, stored_path):
    base = Path(upload_dir).resolve()
    path = (base / stored_path).resolve()
    if path.parent != base:
        raise HTTPException(404, "Фото не найдено")
    if not path.is_file():
        raise HTTPException(404, "Файл фото отсутствует")
    return path
