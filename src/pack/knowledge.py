# pack/knowledge.py

import configparser
from pathlib import Path

import mysql.connector


# =========================================================
# 設定
# =========================================================

DATABASE_CONFIG = Path("config/database.conf")


# =========================================================
# MySQL
# =========================================================

def get_db_connection():
    """
    config/database.conf からMySQL接続情報を読み込み、
    接続オブジェクトを返す。
    """

    if not DATABASE_CONFIG.is_file():
        raise FileNotFoundError(
            f"データベース設定ファイルがありません: {DATABASE_CONFIG}"
        )

    config = configparser.ConfigParser()
    config.read(
        DATABASE_CONFIG,
        encoding="utf-8"
    )

    if "DATABASE" not in config:
        raise RuntimeError(
            "database.conf に [DATABASE] セクションがありません。"
        )

    dbconf = config["DATABASE"]

    return mysql.connector.connect(
        host=dbconf.get(
            "host",
            "localhost"
        ),
        port=dbconf.getint(
            "port",
            3306
        ),
        user=dbconf.get(
            "user",
            "Dail"
        ),
        password=dbconf.get(
            "password",
            ""
        ),
        database=dbconf.get(
            "database",
            "dail"
        )
    )


# =========================================================
# テーブル初期化
# =========================================================

def init_knowledge_table():
    """
    Dail知識データベース用テーブルを作成する。
    既に存在している場合は何もしない。
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge (
                id BIGINT UNSIGNED
                    NOT NULL
                    AUTO_INCREMENT,

                keyword VARCHAR(255)
                    DEFAULT NULL,

                content TEXT
                    NOT NULL,

                source VARCHAR(255)
                    DEFAULT 'conversation',

                importance INT
                    NOT NULL
                    DEFAULT 1,

                created_at TIMESTAMP
                    NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                PRIMARY KEY (id),

                FULLTEXT KEY knowledge_search (
                    keyword,
                    content
                )
            )
            CHARACTER SET utf8mb4
            COLLATE utf8mb4_unicode_ci
            """
        )

        db.commit()

    finally:
        cursor.close()
        db.close()


# =========================================================
# 知識保存
# =========================================================

def save_knowledge(
    content,
    keyword=None,
    source="conversation",
    importance=1
):
    """
    知識をMySQLへ保存する。

    content:
        保存する内容

    keyword:
        検索用キーワード

    source:
        情報源
        manual / conversation / file など

    importance:
        重要度
        大きいほど検索結果で優先される
    """

    if content is None:
        return False

    content = str(content).strip()

    if not content:
        return False

    if keyword is not None:
        keyword = str(keyword).strip()

        if not keyword:
            keyword = None

    try:
        importance = int(importance)

    except (TypeError, ValueError):
        importance = 1

    db = get_db_connection()
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO knowledge (
                keyword,
                content,
                source,
                importance
            )
            VALUES (
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                keyword,
                content,
                source,
                importance
            )
        )

        db.commit()

        return cursor.lastrowid

    finally:
        cursor.close()
        db.close()


# =========================================================
# 知識更新
# =========================================================

def update_knowledge(
    knowledge_id,
    content=None,
    keyword=None,
    source=None,
    importance=None
):
    """
    既存の知識を更新する。
    Noneの項目は変更しない。
    """

    updates = []
    values = []

    if content is not None:
        updates.append("content = %s")
        values.append(str(content).strip())

    if keyword is not None:
        updates.append("keyword = %s")
        values.append(str(keyword).strip())

    if source is not None:
        updates.append("source = %s")
        values.append(str(source).strip())

    if importance is not None:
        updates.append("importance = %s")
        values.append(int(importance))

    if not updates:
        return False

    values.append(int(knowledge_id))

    sql = (
        "UPDATE knowledge SET "
        + ", ".join(updates)
        + " WHERE id = %s"
    )

    db = get_db_connection()
    cursor = db.cursor()

    try:
        cursor.execute(
            sql,
            tuple(values)
        )

        db.commit()

        return cursor.rowcount > 0

    finally:
        cursor.close()
        db.close()


# =========================================================
# 知識削除
# =========================================================

def delete_knowledge(knowledge_id):
    """
    ID指定で知識を削除する。
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM knowledge
            WHERE id = %s
            """,
            (int(knowledge_id),)
        )

        db.commit()

        return cursor.rowcount > 0

    finally:
        cursor.close()
        db.close()


# =========================================================
# ID指定取得
# =========================================================

def get_knowledge(knowledge_id):
    """
    IDを指定して知識を1件取得する。
    """

    db = get_db_connection()
    cursor = db.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                keyword,
                content,
                source,
                importance,
                created_at,
                updated_at

            FROM knowledge

            WHERE id = %s
            """,
            (int(knowledge_id),)
        )

        return cursor.fetchone()

    finally:
        cursor.close()
        db.close()


# =========================================================
# FULLTEXT検索
# =========================================================

def search_knowledge(
    query,
    limit=5
):
    """
    MySQL FULLTEXT検索で関連知識を取得する。
    """

    if query is None:
        return []

    query = str(query).strip()

    if not query:
        return []

    limit = max(
        1,
        min(
            int(limit),
            50
        )
    )

    db = get_db_connection()
    cursor = db.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                keyword,
                content,
                source,
                importance,
                created_at,
                updated_at,

                MATCH(
                    keyword,
                    content
                )
                AGAINST(
                    %s
                    IN NATURAL LANGUAGE MODE
                ) AS score

            FROM knowledge

            WHERE MATCH(
                keyword,
                content
            )
            AGAINST(
                %s
                IN NATURAL LANGUAGE MODE
            )

            ORDER BY
                score DESC,
                importance DESC,
                updated_at DESC

            LIMIT %s
            """,
            (
                query,
                query,
                limit
            )
        )

        return cursor.fetchall()

    finally:
        cursor.close()
        db.close()


# =========================================================
# 単純LIKE検索
# =========================================================

def search_knowledge_like(
    query,
    limit=5
):
    """
    FULLTEXTで拾えない短い文字列などをLIKE検索する。
    """

    if query is None:
        return []

    query = str(query).strip()

    if not query:
        return []

    limit = max(
        1,
        min(
            int(limit),
            50
        )
    )

    pattern = f"%{query}%"

    db = get_db_connection()
    cursor = db.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                keyword,
                content,
                source,
                importance,
                created_at,
                updated_at

            FROM knowledge

            WHERE
                keyword LIKE %s
                OR
                content LIKE %s

            ORDER BY
                importance DESC,
                updated_at DESC

            LIMIT %s
            """,
            (
                pattern,
                pattern,
                limit
            )
        )

        return cursor.fetchall()

    finally:
        cursor.close()
        db.close()


# =========================================================
# 検索統合
# =========================================================

def find_knowledge(
    query,
    limit=5
):
    """
    FULLTEXT検索を行い、
    結果が無ければLIKE検索へフォールバックする。
    """

    results = search_knowledge(
        query,
        limit=limit
    )

    if results:
        return results

    return search_knowledge_like(
        query,
        limit=limit
    )


# =========================================================
# Gemini向けコンテキスト
# =========================================================

def get_knowledge_context(
    query,
    limit=5
):
    """
    ユーザー入力に関連する知識を検索して、
    GeminiのSystem Promptへ追加できる文字列を返す。
    """

    results = find_knowledge(
        query,
        limit=limit
    )

    if not results:
        return ""

    lines = [
        "===== Dail Knowledge =====",
        "",
        "以下はDailが保存している長期知識です。",
        "ユーザーの現在の質問に関連する場合のみ参考にしてください。",
        "保存情報が現在のユーザー入力と矛盾する場合は、"
        "現在のユーザー入力を優先してください。",
        ""
    ]

    for item in results:

        lines.append(
            f"[Knowledge ID: {item['id']}]"
        )

        if item.get("keyword"):
            lines.append(
                f"Keyword: {item['keyword']}"
            )

        if item.get("source"):
            lines.append(
                f"Source: {item['source']}"
            )

        lines.append(
            f"Importance: {item['importance']}"
        )

        lines.append("")

        lines.append(
            item["content"]
        )

        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(
        "===== End Dail Knowledge ====="
    )

    return "\n".join(lines)


# =========================================================
# 一覧
# =========================================================

def list_knowledge(
    limit=20
):
    """
    保存されている知識を新しい順に取得する。
    """

    limit = max(
        1,
        min(
            int(limit),
            100
        )
    )

    db = get_db_connection()
    cursor = db.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                keyword,
                content,
                source,
                importance,
                created_at,
                updated_at

            FROM knowledge

            ORDER BY
                updated_at DESC

            LIMIT %s
            """,
            (limit,)
        )

        return cursor.fetchall()

    finally:
        cursor.close()
        db.close()


# =========================================================
# 件数
# =========================================================

def count_knowledge():
    """
    保存されている知識数を返す。
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM knowledge
            """
        )

        result = cursor.fetchone()

        return int(result[0])

    finally:
        cursor.close()
        db.close()


# =========================================================
# 単体動作確認
# =========================================================

if __name__ == "__main__":

    print(
        "Dail Knowledge Database"
    )

    print(
        "テーブルを確認しています..."
    )

    init_knowledge_table()

    print(
        "Knowledge Table: OK"
    )

    print(
        f"Knowledge Count: "
        f"{count_knowledge()}"
    )
