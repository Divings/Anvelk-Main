from pathlib import Path
import uuid
import mysql.connector


DEVICE_FILE = Path("/etc/averia/device_id.conf")


def get_device_id():
    """
    この端末固有のIDを取得。
    初回起動時のみUUIDを生成して保存する。
    """
    DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if DEVICE_FILE.exists():
        device_id = DEVICE_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if device_id:
            return device_id

    device_id = str(uuid.uuid4())

    DEVICE_FILE.write_text(
        device_id,
        encoding="utf-8"
    )

    return device_id


def get_last_device_id(conn, username):
    """
    DBから前回利用端末IDを取得。
    """
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT last_device_id
        FROM auth_cards
        WHERE username = %s
        """,
        (username,)
    )

    row = cursor.fetchone()
    cursor.close()

    if row:
        return row[0]

    return None


def update_last_device_id(conn, username, device_id):
    """
    今回利用した端末IDを保存。
    """
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE auth_cards
        SET last_device_id = %s
        WHERE username = %s
        """,
        (device_id, username)
    )

    conn.commit()
    cursor.close()


def check_device_change(conn, username):
    """
    前回とは異なる端末の場合、
    アヴェリアへ渡すNoteを返す。
    """
    current_device_id = get_device_id()
    last_device_id = get_last_device_id(
        conn,
        username
    )

    note = None

    if (
        last_device_id is not None
        and last_device_id != current_device_id
    ):
        note = (
            "Note: 前回とは異なる端末で利用されています。"
            "端末固有のファイルやパスは、"
            "この端末には存在しない場合があります。"
        )

    update_last_device_id(
        conn,
        username,
        current_device_id
    )

    return note