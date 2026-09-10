from pathlib import Path
from PIL import Image
import pytesseract


def ocr_image(
    image_path: str,
    lang: str = "jpn+eng",
) -> dict:
    path = Path(image_path)

    if not path.exists():
        return {
            "success": False,
            "error": "file_not_found",
            "message": f"ファイルが見つかりません: {image_path}",
        }

    try:
        image = Image.open(path)

        text = pytesseract.image_to_string(
            image,
            lang=lang,
        )

        return {
            "success": True,
            "file": str(path),
            "text": text.strip(),
        }

    except Exception as e:
        return {
            "success": False,
            "error": "ocr_failed",
            "message": str(e),
        }