from pathlib import Path

MAX_FILE_SIZE_MB = 50
MAX_PAGES_PER_READ = 100
MAX_TEXT_CHARS = 200_000


def _load_pypdf():
    try:
        from pypdf import PdfReader
        return PdfReader
    except ImportError as e:
        raise RuntimeError(
            "pypdf がインストールされていません。pip install pypdf を実行してください。"
        ) from e


def _validate_pdf_path(pdf_path):
    path = Path(str(pdf_path)).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"PDFファイルが存在しません: {path}")
    if not path.is_file():
        raise ValueError(f"指定されたパスはファイルではありません: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"PDFファイルではありません: {path}")
    size = path.stat().st_size
    if size > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise ValueError(
            f"PDFファイルが大きすぎます。最大{MAX_FILE_SIZE_MB}MBです。"
        )
    return path


def get_pdf_info(pdf_path):
    try:
        path = _validate_pdf_path(pdf_path)
        PdfReader = _load_pypdf()
        reader = PdfReader(str(path))
        metadata = reader.metadata
        return {
            "success": True,
            "filename": path.name,
            "path": str(path),
            "file_size_bytes": path.stat().st_size,
            "file_size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "pages": len(reader.pages),
            "title": getattr(metadata, "title", None) if metadata else None,
            "author": getattr(metadata, "author", None) if metadata else None,
            "encrypted": bool(reader.is_encrypted),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_pdf(pdf_path, start_page=1, end_page=None):
    try:
        path = _validate_pdf_path(pdf_path)
        PdfReader = _load_pypdf()
        reader = PdfReader(str(path))

        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                return {
                    "success": False,
                    "error": "パスワードで保護されたPDFは読み取れません。"
                }

        total_pages = len(reader.pages)
        if total_pages == 0:
            return {"success": False, "error": "PDFにページがありません。"}

        start_page = int(start_page)
        if start_page < 1 or start_page > total_pages:
            return {
                "success": False,
                "error": f"start_pageは1〜{total_pages}で指定してください。"
            }

        if end_page is None:
            end_page = total_pages
        else:
            end_page = min(int(end_page), total_pages)

        if end_page < start_page:
            return {"success": False, "error": "end_pageはstart_page以上にしてください。"}

        page_count = end_page - start_page + 1
        if page_count > MAX_PAGES_PER_READ:
            return {
                "success": False,
                "error": (
                    f"一度に読み取れるのは最大{MAX_PAGES_PER_READ}ページです。"
                    "ページ範囲を分割してください。"
                )
            }

        chunks = []
        empty_pages = []
        truncated = False
        current_chars = 0

        for page_number in range(start_page, end_page + 1):
            try:
                text = reader.pages[page_number - 1].extract_text() or ""
                text = text.strip()
            except Exception as e:
                text = f"[ページの読み取りに失敗しました: {e}]"

            if not text:
                empty_pages.append(page_number)
                text = "[テキストを取得できませんでした]"

            block = f"===== Page {page_number} =====\n{text}"
            remaining = MAX_TEXT_CHARS - current_chars
            if remaining <= 0:
                truncated = True
                break
            if len(block) > remaining:
                block = block[:remaining]
                truncated = True

            chunks.append(block)
            current_chars += len(block)
            if truncated:
                break

        return {
            "success": True,
            "filename": path.name,
            "total_pages": total_pages,
            "start_page": start_page,
            "end_page": end_page,
            "empty_text_pages": empty_pages,
            "truncated": truncated,
            "text": "\n\n".join(chunks),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
