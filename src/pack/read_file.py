import os


MAX_READ_FILE_SIZE = 1024 * 1024  # 1MB


def read_local_file(path):
    """
    指定されたローカルテキストファイルを読み込む。
    ファイルの変更・実行は行わない。
    """

    try:
        real_path = os.path.realpath(os.path.expanduser(path))

        if not os.path.exists(real_path):
            return {
                "success": False,
                "error": "file_not_found",
                "path": real_path,
            }

        if not os.path.isfile(real_path):
            return {
                "success": False,
                "error": "not_a_file",
                "path": real_path,
            }

        file_size = os.path.getsize(real_path)

        if file_size > MAX_READ_FILE_SIZE:
            return {
                "success": False,
                "error": "file_too_large",
                "path": real_path,
                "size": file_size,
                "max_size": MAX_READ_FILE_SIZE,
            }

        with open(real_path, "r", encoding="utf-8") as f:
            content = f.read()

        return {
            "success": True,
            "path": real_path,
            "size": file_size,
            "content": content,
        }

    except PermissionError:
        return {
            "success": False,
            "error": "permission_denied",
            "path": path,
        }

    except UnicodeDecodeError:
        return {
            "success": False,
            "error": "not_utf8_text",
            "path": path,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "path": path,
        }