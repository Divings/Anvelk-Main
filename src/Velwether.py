import Velwether_Core
import traceback
import sys
from  pack.Auth import *


sys.stdin.reconfigure(
    encoding="utf-8",
    errors="replace"
)

sys.stdout.reconfigure(
    encoding="utf-8",
    errors="replace"
)

sys.stderr.reconfigure(
    encoding="utf-8",
    errors="replace"
)

auth_responce = authorize_user()

if not auth_responce["ok"]:
    print("")
    print(" アヴェリアを起動できません。")
    print(f" 理由: {auth_responce['reason']}")
    print(f" ユーザー: {auth_responce['username']}")

    if "error" in auth_responce:
        print(f" 詳細: {auth_responce['error']}")

    sys.exit(1)

auth_result = authorize_environment()

if not auth_result["ok"]:
    print("")
    print(" アヴェリアを起動できません。")
    print(f" 理由: {auth_result['reason']}")

    if "username" in auth_result:
        print(f" ユーザー: {auth_result['username']}")

    if auth_result["reason"] == "library_version_mismatch":
        print(
            f" 必要なライブラリバージョン: "
            f"{auth_result['expected_version']}"
        )
        print(
            f" 現在のライブラリバージョン: "
            f"{auth_result['version']['string']}"
        )

    if "error" in auth_result:
        print(f" 詳細: {auth_result['error']}")

    raise SystemExit(1)

try:
    Velwether_Core.main()
except Exception as e:
    print("")
    print(" 予期せぬエラーが発生しました。")
    print(f" {type(e).__name__}: {e}")
    traceback.print_exc()
