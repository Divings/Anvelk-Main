from pathlib import Path
import shutil
import os

FOLDER_ROOT = Path("/mnt/Folders").resolve()

IMPORTANCE_DIRS = {
    "normal": "Normal",
    "important": "Important",
    "critical": "Critical",
}


def sort_file_by_importance(
    file_path: str,
    importance: str,
) -> dict:

    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {
            "success": False,
            "error": "file_not_found",
            "message": f"ファイルが見つかりません: {file_path}",
        }

    if not source.is_file():
        return {
            "success": False,
            "error": "not_a_file",
            "message": "指定されたパスはファイルではありません。",
        }

    importance = str(importance).strip().lower()

    if importance not in IMPORTANCE_DIRS:
        return {
            "success": False,
            "error": "invalid_importance",
            "message": (
                "importance は normal / important / critical "
                "のいずれかを指定してください。"
            ),
        }

    destination_dir = (
        FOLDER_ROOT / IMPORTANCE_DIRS[importance]
    ).resolve()

    try:
        destination_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
    except Exception as e:
        return {
            "success": False,
            "error": "mkdir_failed",
            "message": str(e),
        }

    destination = destination_dir / source.name

    count = 1

    while destination.exists():
        destination = (
            destination_dir
            / f"{source.stem}_{count}{source.suffix}"
        )
        count += 1

    try:
        shutil.move(
            str(source),
            str(destination),
        )

        return {
            "success": True,
            "importance": importance,
            "folder": IMPORTANCE_DIRS[importance],
            "source": str(source),
            "destination": str(destination),
        }

    except Exception as e:
        return {
            "success": False,
            "error": "move_failed",
            "message": str(e),
        }