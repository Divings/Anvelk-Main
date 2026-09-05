from pathlib import Path


HOME_DIR = Path.home().resolve()


def _is_allowed_path(target: Path) -> bool:
    """
    書き込み先がホームディレクトリ配下か確認する。
    """
    try:
        target.relative_to(HOME_DIR)
        return True
    except ValueError:
        return False


def write_local_file(
    path: str,
    content: str,
    overwrite: bool = False,
):
    """
    ホームディレクトリ配下へUTF-8テキストを書き込む。

    Parameters
    ----------
    path : str
        書き込み先ファイルパス

    content : str
        書き込む内容

    overwrite : bool
        既存ファイルを上書きする場合はTrue

    Returns
    -------
    dict
        {
            "success": bool,
            "path": str,
            ...
        }
    """

    try:
        target = Path(path).expanduser().resolve()

        # ホーム外への書き込みを拒否
        if not _is_allowed_path(target):
            return {
                "success": False,
                "error": (
                    "ホームディレクトリ配下以外には"
                    "書き込めません。"
                ),
                "path": str(target),
            }

        existed_before = target.exists()

        # ディレクトリ指定は拒否
        if existed_before and target.is_dir():
            return {
                "success": False,
                "error": "指定されたパスはディレクトリです。",
                "path": str(target),
            }

        # 既存ファイルは明示的な上書き許可が必要
        if existed_before and not overwrite:
            return {
                "success": False,
                "error": (
                    "ファイルが既に存在します。"
                    "上書きする場合は overwrite=true "
                    "を指定してください。"
                ),
                "path": str(target),
            }

        # 親ディレクトリが無ければ作成
        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        data = str(content)

        target.write_text(
            data,
            encoding="utf-8",
        )

        return {
            "success": True,
            "path": str(target),
            "bytes": len(data.encode("utf-8")),
            "overwritten": existed_before,
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "path": path,
        }