# pack/auto_learning.py

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
    database.conf から自動学習設定を取得する。
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

    return {
        "enabled": enabled,
        "importance_default": importance_default
    }


def auto_learning_enabled():
    """
    自動学習機能が有効か確認する。
    """

    return load_learning_config()["enabled"]


def should_check_learning(user_text):
    """
    学習キーワードが含まれているか確認する。

    キーワードが無い場合は
    Gemini APIを呼ばない。
    """

    if not user_text:
        return False

    text = str(user_text).strip()

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
    Geminiへ送る軽量な学習判定プロンプト。
    """

    return f"""
次の会話から、今後も役立つ長期的な事実だけ抽出してください。

雑談、一時的な情報、推測、秘密情報は保存しないでください。

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


def parse_learning_response(response_text):
    """
    GeminiのJSON応答を解析する。
    """

    if not response_text:
        return None

    text = str(
        response_text
    ).strip()

    # ```json ... ``` 対策
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
        data = json.loads(text)

    except json.JSONDecodeError:
        return None

    if not isinstance(
        data,
        dict
    ):
        return None

    return data


def is_duplicate_knowledge(content):
    """
    完全に同じ知識が既に存在するか確認する。
    """

    if not content:
        return True

    try:
        results = find_knowledge(
            content,
            limit=3
        )

    except Exception:
        return False

    normalized = (
        str(content)
        .strip()
        .lower()
    )

    for item in results:

        existing = (
            item.get(
                "content",
                ""
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
    会話から長期知識を抽出して保存する。

    条件:
    1. database.conf の enabled=true
    2. ユーザー入力に学習キーワードが含まれる

    上記を満たす場合のみ
    Gemini APIを追加で呼び出す。
    """

    settings = (
        load_learning_config()
    )

    # ==========================================
    # 自動学習OFF
    # ==========================================

    if not settings["enabled"]:

        return {
            "enabled": False,
            "saved": False,
            "reason": "disabled"
        }

    # ==========================================
    # 空入力
    # ==========================================

    if not user_text:

        return {
            "enabled": True,
            "saved": False,
            "reason": "empty"
        }

    # ==========================================
    # 学習キーワード確認
    # Gemini API節約ポイント
    # ==========================================

    if not should_check_learning(
        user_text
    ):

        return {
            "enabled": True,
            "saved": False,
            "reason": "no_learning_keyword"
        }

    # ==========================================
    # Gemini用プロンプト
    # ==========================================

    prompt = build_learning_prompt(
        user_text,
        assistant_text,
        default_importance=(
            settings[
                "importance_default"
            ]
        )
    )

    # ==========================================
    # Gemini API
    # ==========================================

    try:
        response = gemini_func(
            prompt
        )

    except Exception as e:

        return {
            "enabled": True,
            "saved": False,
            "reason": "api_error",
            "error": str(e)
        }

    # ==========================================
    # JSON解析
    # ==========================================

    data = parse_learning_response(
        response
    )

    if not data:

        return {
            "enabled": True,
            "saved": False,
            "reason": "invalid_response"
        }

    # ==========================================
    # 保存不要判定
    # ==========================================

    if not data.get(
        "save",
        False
    ):

        return {
            "enabled": True,
            "saved": False,
            "reason": "not_needed"
        }

    # ==========================================
    # 保存内容取得
    # ==========================================

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
            "saved": False,
            "reason": "empty_content"
        }

    # ==========================================
    # 重複確認
    # ==========================================

    if is_duplicate_knowledge(
        content
    ):

        return {
            "enabled": True,
            "saved": False,
            "reason": "duplicate"
        }

    # ==========================================
    # importance
    # ==========================================

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

    # ==========================================
    # MySQL保存
    # ==========================================

    try:
        knowledge_id = (
            save_knowledge(
                keyword=keyword,
                content=content,
                source="auto_conversation",
                importance=importance
            )
        )

    except Exception as e:

        return {
            "enabled": True,
            "saved": False,
            "reason": "database_error",
            "error": str(e)
        }

    return {
        "enabled": True,
        "saved": True,
        "knowledge_id": knowledge_id,
        "keyword": keyword,
        "content": content,
        "importance": importance
    }
