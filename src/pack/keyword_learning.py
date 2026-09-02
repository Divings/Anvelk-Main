# pack/keyword_learning.py

import json
import configparser
from pathlib import Path

from pack.knowledge import (
    save_knowledge,
    find_knowledge
)


DATABASE_CONFIG = Path("config/database.conf")


LEARNING_KEYWORDS = [
    "覚えて",
    "記憶して",
    "忘れないで",
    "今後は",
    "これからは",
    "保存して",
    "学習して"
]


def load_learning_config():
    """
    database.conf から学習機能設定を取得する。
    """

    config = configparser.ConfigParser()

    config.read(
        DATABASE_CONFIG,
        encoding="utf-8"
    )

    if "LEARNING" not in config:
        return {
            "enabled": False,
            "importance_default": 3
        }

    section = config["LEARNING"]

    try:
        enabled = section.getboolean(
            "enabled",
            fallback=False
        )

    except ValueError:
        enabled = False

    try:
        importance_default = section.getint(
            "importance_default",
            fallback=3
        )

    except ValueError:
        importance_default = 3

    importance_default = max(
        1,
        min(
            importance_default,
            5
        )
    )

    return {
        "enabled": enabled,
        "importance_default": importance_default
    }


def learning_enabled():
    """
    学習機能そのものが有効か確認する。
    """

    return load_learning_config()["enabled"]


def contains_learning_keyword(user_text):
    """
    ユーザー入力に学習キーワードが含まれるか確認する。

    キーワードが無い場合、
    Gemini APIは追加で呼び出さない。
    """

    if not user_text:
        return False

    text = str(
        user_text
    ).strip()

    return any(
        keyword in text
        for keyword in LEARNING_KEYWORDS
    )


def build_learning_prompt(
    user_text,
    assistant_text,
    default_importance=3
):
    """
    学習内容抽出用の軽量プロンプト。
    """

    return f"""
次の会話から、ユーザーが覚えるよう求めた長期的な事実を抽出してください。

推測や秘密情報は保存しないでください。

JSONのみ返してください。

保存不要:
{{"save":false}}

保存:
{{"save":true,"keyword":"短い語","content":"保存する事実","importance":{default_importance}}}

importanceは1〜5。

User:
{user_text}

Dail:
{assistant_text}
""".strip()


def parse_learning_response(
    response_text
):
    """
    GeminiのJSON応答を解析する。
    """

    if not response_text:
        return None

    text = str(
        response_text
    ).strip()

    # ```json
    # {...}
    # ```
    # のような応答にも対応
    if text.startswith("```"):

        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if (
            lines
            and
            lines[-1].strip() == "```"
        ):
            lines = lines[:-1]

        text = "\n".join(
            lines
        ).strip()

    try:
        data = json.loads(
            text
        )

    except json.JSONDecodeError:
        return None

    if not isinstance(
        data,
        dict
    ):
        return None

    return data


def is_duplicate_knowledge(
    content
):
    """
    同じ内容が既に保存されているか確認する。
    """

    if not content:
        return True

    normalized = (
        str(content)
        .strip()
        .lower()
    )

    try:
        results = find_knowledge(
            content,
            limit=5
        )

    except Exception:
        return False

    for item in results:

        existing = (
            str(
                item.get(
                    "content",
                    ""
                )
            )
            .strip()
            .lower()
        )

        if existing == normalized:
            return True

    return False


def learn_from_conversation(
    user_text,
    assistant_text,
    gemini_func
):
    """
    キーワード指定された会話だけ学習する。

    処理条件:

    1. database.conf の
       [LEARNING] enabled=true

    2. ユーザー入力に
       LEARNING_KEYWORDS のどれかが含まれる

    条件を満たさない場合は
    Gemini APIを追加で呼び出さない。
    """

    settings = (
        load_learning_config()
    )

    # -----------------------------------------
    # 学習機能OFF
    # -----------------------------------------

    if not settings["enabled"]:

        return {
            "enabled": False,
            "triggered": False,
            "saved": False,
            "reason": "disabled"
        }

    # -----------------------------------------
    # 空入力
    # -----------------------------------------

    if not user_text:

        return {
            "enabled": True,
            "triggered": False,
            "saved": False,
            "reason": "empty"
        }

    # -----------------------------------------
    # キーワード判定
    # -----------------------------------------

    if not contains_learning_keyword(
        user_text
    ):

        return {
            "enabled": True,
            "triggered": False,
            "saved": False,
            "reason": "no_keyword"
        }

    # ここまで来た場合のみ
    # Gemini APIを追加で使用する

    prompt = build_learning_prompt(
        user_text,
        assistant_text,
        default_importance=(
            settings[
                "importance_default"
            ]
        )
    )

    # -----------------------------------------
    # Gemini API
    # -----------------------------------------

    try:
        response = gemini_func(
            prompt
        )

    except Exception as e:

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "api_error",
            "error": str(e)
        }

    # -----------------------------------------
    # JSON解析
    # -----------------------------------------

    data = parse_learning_response(
        response
    )

    if not data:

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "invalid_response"
        }

    # -----------------------------------------
    # Gemini側が保存不要と判断
    # -----------------------------------------

    if not data.get(
        "save",
        False
    ):

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "not_needed"
        }

    # -----------------------------------------
    # 保存内容
    # -----------------------------------------

    keyword = str(
        data.get(
            "keyword",
            ""
        )
    ).strip()

    content = str(
        data.get(
            "content",
            ""
        )
    ).strip()

    if not content:

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "empty_content"
        }

    # -----------------------------------------
    # 重複チェック
    # -----------------------------------------

    if is_duplicate_knowledge(
        content
    ):

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "duplicate"
        }

    # -----------------------------------------
    # importance
    # -----------------------------------------

    try:
        importance = int(
            data.get(
                "importance",
                settings[
                    "importance_default"
                ]
            )
        )

    except (
        TypeError,
        ValueError
    ):
        importance = (
            settings[
                "importance_default"
            ]
        )

    importance = max(
        1,
        min(
            importance,
            5
        )
    )

    # -----------------------------------------
    # MySQL保存
    # -----------------------------------------

    try:
        knowledge_id = (
            save_knowledge(
                keyword=keyword,
                content=content,
                source="keyword_conversation",
                importance=importance
            )
        )

    except Exception as e:

        return {
            "enabled": True,
            "triggered": True,
            "saved": False,
            "reason": "database_error",
            "error": str(e)
        }

    return {
        "enabled": True,
        "triggered": True,
        "saved": True,
        "knowledge_id": knowledge_id,
        "keyword": keyword,
        "content": content,
        "importance": importance
    }
