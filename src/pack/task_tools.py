import datetime
import mysql.connector


# =========================================================
# STATUS
# =========================================================

STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3
STATUS_DISABLED = 4


STATUS_NAMES = {
    STATUS_PENDING: "pending",
    STATUS_RUNNING: "running",
    STATUS_SUCCESS: "success",
    STATUS_FAILED: "failed",
    STATUS_DISABLED: "disabled",
}


# =========================================================
# WEEKDAY
#
# Python datetime.weekday()
# 0 = Monday
# 6 = Sunday
# =========================================================

WEEKDAY_MAP = {
    "monday": 0,
    "mon": 0,
    "月": 0,
    "月曜": 0,
    "月曜日": 0,

    "tuesday": 1,
    "tue": 1,
    "火": 1,
    "火曜": 1,
    "火曜日": 1,

    "wednesday": 2,
    "wed": 2,
    "水": 2,
    "水曜": 2,
    "水曜日": 2,

    "thursday": 3,
    "thu": 3,
    "木": 3,
    "木曜": 3,
    "木曜日": 3,

    "friday": 4,
    "fri": 4,
    "金": 4,
    "金曜": 4,
    "金曜日": 4,

    "saturday": 5,
    "sat": 5,
    "土": 5,
    "土曜": 5,
    "土曜日": 5,

    "sunday": 6,
    "sun": 6,
    "日": 6,
    "日曜": 6,
    "日曜日": 6,
}


WEEKDAY_NAMES = {
    0: "月曜日",
    1: "火曜日",
    2: "水曜日",
    3: "木曜日",
    4: "金曜日",
    5: "土曜日",
    6: "日曜日",
}


# =========================================================
# DB接続
# =========================================================

def get_connection(db_config):
    """
    db_config例:

    {
        "host": "localhost",
        "port": 3306,
        "user": "avelia",
        "password": "...",
        "database": "avelia"
    }
    """

    return mysql.connector.connect(
        host=db_config["host"],
        port=int(
            db_config.get(
                "port",
                3306
            )
        ),
        user=db_config["user"],
        password=db_config["password"],
        database=db_config["database"],
        charset="utf8mb4",
        autocommit=False,
    )


# =========================================================
# 日付正規化
# =========================================================

def normalize_date(value):
    """
    YYYY-MM-DD
    datetime.date
    datetime.datetime
    を受け付ける。
    """

    if isinstance(
        value,
        datetime.datetime
    ):
        return value.date().isoformat()

    if isinstance(
        value,
        datetime.date
    ):
        return value.isoformat()

    value = str(
        value
    ).strip()

    try:
        parsed = (
            datetime.datetime.strptime(
                value,
                "%Y-%m-%d"
            )
        )

    except ValueError as exc:
        raise ValueError(
            "日付は YYYY-MM-DD 形式で指定してください"
        ) from exc

    return parsed.strftime(
        "%Y-%m-%d"
    )


# =========================================================
# 時刻正規化
# =========================================================

def normalize_time(value):
    """
    HH:MM
    HH:MM:SS
    datetime.time
    を受け付ける。
    """

    if isinstance(
        value,
        datetime.time
    ):
        return value.strftime(
            "%H:%M:%S"
        )

    value = str(
        value
    ).strip()

    for fmt in (
        "%H:%M",
        "%H:%M:%S",
    ):

        try:
            parsed = (
                datetime.datetime.strptime(
                    value,
                    fmt
                )
            )

            return parsed.strftime(
                "%H:%M:%S"
            )

        except ValueError:
            continue

    raise ValueError(
        "時刻は HH:MM または "
        "HH:MM:SS 形式で指定してください"
    )


# =========================================================
# 曜日正規化
# =========================================================

def normalize_weekday(value):

    if isinstance(
        value,
        int
    ):
        if 0 <= value <= 6:
            return value

        raise ValueError(
            "weekday_code は0〜6で指定してください"
        )

    key = (
        str(value)
        .strip()
        .lower()
    )

    if key not in WEEKDAY_MAP:
        raise ValueError(
            f"不明な曜日です: {value}"
        )

    return WEEKDAY_MAP[key]


# =========================================================
# MySQL TIME → 文字列
# =========================================================

def serialize_time(value):

    if value is None:
        return None

    if isinstance(
        value,
        datetime.time
    ):
        return value.strftime(
            "%H:%M:%S"
        )

    if isinstance(
        value,
        datetime.timedelta
    ):
        total_seconds = int(
            value.total_seconds()
        )

        hours = (
            total_seconds
            // 3600
        )

        minutes = (
            total_seconds
            % 3600
        ) // 60

        seconds = (
            total_seconds
            % 60
        )

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return str(
        value
    )


# =========================================================
# DB行 → JSON化可能なdict
# =========================================================

def serialize_task_row(row):

    if row is None:
        return None

    result = dict(
        row
    )

    status = result.get(
        "status"
    )

    result["status_name"] = (
        STATUS_NAMES.get(
            status,
            "unknown"
        )
    )

    weekday_code = result.get(
        "weekday_code"
    )

    if weekday_code is not None:
        result["weekday_name"] = (
            WEEKDAY_NAMES.get(
                weekday_code
            )
        )
    else:
        result["weekday_name"] = None

    run_date = result.get(
        "run_date"
    )

    if isinstance(
        run_date,
        datetime.datetime
    ):
        result["run_date"] = (
            run_date.date().isoformat()
        )

    elif isinstance(
        run_date,
        datetime.date
    ):
        result["run_date"] = (
            run_date.isoformat()
        )

    result["run_time"] = (
        serialize_time(
            result.get(
                "run_time"
            )
        )
    )

    for field in (
        "last_run_at",
        "created_at",
        "updated_at",
    ):

        value = result.get(
            field
        )

        if isinstance(
            value,
            datetime.datetime
        ):
            result[field] = (
                value.isoformat(
                    sep=" "
                )
            )

    return result


# =========================================================
# 単発タスク追加
# =========================================================

def add_one_shot_task(
    db_config,
    task_name,
    program_path,
    run_date,
    run_time,
    arguments=None,
    working_directory=None,
):

    task_name = str(
        task_name
    ).strip()

    program_path = str(
        program_path
    ).strip()

    if not task_name:
        raise ValueError(
            "task_name が空です"
        )

    if not program_path:
        raise ValueError(
            "program_path が空です"
        )

    run_date = (
        normalize_date(
            run_date
        )
    )

    run_time = (
        normalize_time(
            run_time
        )
    )

    sql = """
        INSERT INTO scheduled_tasks (
            task_name,
            program_path,
            arguments,
            working_directory,

            run_date,
            run_time,

            is_one_shot,
            is_weekly,

            weekday_code,

            status
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,

            %s,
            %s,

            1,
            0,

            NULL,

            %s
        )
    """

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            sql,
            (
                task_name,
                program_path,
                arguments,
                working_directory,

                run_date,
                run_time,

                STATUS_PENDING,
            ),
        )

        task_id = (
            cursor.lastrowid
        )

        conn.commit()

        return {
            "success": True,
            "task_id": task_id,

            "task_name": task_name,

            "program_path": (
                program_path
            ),

            "arguments": (
                arguments
            ),

            "working_directory": (
                working_directory
            ),

            "run_date": (
                run_date
            ),

            "run_time": (
                run_time
            ),

            "status": (
                STATUS_PENDING
            ),

            "status_name": (
                "pending"
            ),

            "type": (
                "one_shot"
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# 毎週タスク追加
# =========================================================

def add_weekly_task(
    db_config,
    task_name,
    program_path,
    weekday,
    run_time,
    arguments=None,
    working_directory=None,
):

    task_name = str(
        task_name
    ).strip()

    program_path = str(
        program_path
    ).strip()

    if not task_name:
        raise ValueError(
            "task_name が空です"
        )

    if not program_path:
        raise ValueError(
            "program_path が空です"
        )

    weekday_code = (
        normalize_weekday(
            weekday
        )
    )

    run_time = (
        normalize_time(
            run_time
        )
    )

    sql = """
        INSERT INTO scheduled_tasks (
            task_name,
            program_path,
            arguments,
            working_directory,

            run_date,
            run_time,

            is_one_shot,
            is_weekly,

            weekday_code,

            status
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,

            NULL,
            %s,

            0,
            1,

            %s,

            %s
        )
    """

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            sql,
            (
                task_name,
                program_path,
                arguments,
                working_directory,

                run_time,

                weekday_code,

                STATUS_PENDING,
            ),
        )

        task_id = (
            cursor.lastrowid
        )

        conn.commit()

        return {
            "success": True,

            "task_id": (
                task_id
            ),

            "task_name": (
                task_name
            ),

            "program_path": (
                program_path
            ),

            "arguments": (
                arguments
            ),

            "working_directory": (
                working_directory
            ),

            "weekday_code": (
                weekday_code
            ),

            "weekday_name": (
                WEEKDAY_NAMES[
                    weekday_code
                ]
            ),

            "run_time": (
                run_time
            ),

            "status": (
                STATUS_PENDING
            ),

            "status_name": (
                "pending"
            ),

            "type": (
                "weekly"
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# タスク一覧
# =========================================================

def list_tasks(
    db_config,
    include_disabled=False,
):

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        if include_disabled:

            cursor.execute(
                """
                SELECT *
                FROM scheduled_tasks

                ORDER BY
                    id DESC
                """
            )

        else:

            cursor.execute(
                """
                SELECT *
                FROM scheduled_tasks

                WHERE status != %s

                ORDER BY
                    id DESC
                """,
                (
                    STATUS_DISABLED,
                ),
            )

        rows = (
            cursor.fetchall()
        )

        return [
            serialize_task_row(
                row
            )
            for row in rows
        ]

    finally:

        cursor.close()
        conn.close()


# =========================================================
# pendingタスク一覧
# =========================================================

def list_pending_tasks(
    db_config
):

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT *
            FROM scheduled_tasks

            WHERE status = %s

            ORDER BY
                run_date ASC,
                run_time ASC,
                id ASC
            """,
            (
                STATUS_PENDING,
            ),
        )

        rows = (
            cursor.fetchall()
        )

        return [
            serialize_task_row(
                row
            )
            for row in rows
        ]

    finally:

        cursor.close()
        conn.close()


# =========================================================
# ID指定取得
# =========================================================

def get_task(
    db_config,
    task_id
):

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT *
            FROM scheduled_tasks

            WHERE id = %s
            """,
            (
                int(
                    task_id
                ),
            ),
        )

        row = (
            cursor.fetchone()
        )

        return (
            serialize_task_row(
                row
            )
        )

    finally:

        cursor.close()
        conn.close()


# =========================================================
# 無効化
# =========================================================

def disable_task(
    db_config,
    task_id
):

    task_id = int(
        task_id
    )

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE scheduled_tasks

            SET
                status = %s

            WHERE
                id = %s
                AND status != %s
            """,
            (
                STATUS_DISABLED,
                task_id,
                STATUS_RUNNING,
            ),
        )

        if cursor.rowcount == 0:

            conn.rollback()

            return {
                "success": False,
                "task_id": task_id,
                "message": (
                    "タスクが存在しないか、"
                    "現在実行中です"
                ),
            }

        conn.commit()

        return {
            "success": True,
            "task_id": task_id,

            "status": (
                STATUS_DISABLED
            ),

            "status_name": (
                "disabled"
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# 再有効化
# =========================================================

def enable_task(
    db_config,
    task_id
):

    task_id = int(
        task_id
    )

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE scheduled_tasks

            SET
                status = %s,
                last_error = NULL

            WHERE
                id = %s
                AND status = %s
            """,
            (
                STATUS_PENDING,
                task_id,
                STATUS_DISABLED,
            ),
        )

        if cursor.rowcount == 0:

            conn.rollback()

            return {
                "success": False,
                "task_id": task_id,
                "message": (
                    "disabled状態の"
                    "タスクが見つかりません"
                ),
            }

        conn.commit()

        return {
            "success": True,
            "task_id": task_id,

            "status": (
                STATUS_PENDING
            ),

            "status_name": (
                "pending"
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# failed → pending
# =========================================================

def retry_task(
    db_config,
    task_id
):

    task_id = int(
        task_id
    )

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE scheduled_tasks

            SET
                status = %s,
                last_exit_code = NULL,
                last_error = NULL

            WHERE
                id = %s
                AND status = %s
            """,
            (
                STATUS_PENDING,
                task_id,
                STATUS_FAILED,
            ),
        )

        if cursor.rowcount == 0:

            conn.rollback()

            return {
                "success": False,
                "task_id": task_id,
                "message": (
                    "failed状態の"
                    "タスクが見つかりません"
                ),
            }

        conn.commit()

        return {
            "success": True,

            "task_id": (
                task_id
            ),

            "status": (
                STATUS_PENDING
            ),

            "status_name": (
                "pending"
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# タスク削除
# =========================================================

def delete_task(
    db_config,
    task_id
):

    task_id = int(
        task_id
    )

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            DELETE FROM scheduled_tasks

            WHERE
                id = %s
                AND status != %s
            """,
            (
                task_id,
                STATUS_RUNNING,
            ),
        )

        if cursor.rowcount == 0:

            conn.rollback()

            return {
                "success": False,
                "task_id": task_id,
                "message": (
                    "タスクが存在しないか、"
                    "現在実行中です"
                ),
            }

        conn.commit()

        return {
            "success": True,

            "task_id": (
                task_id
            ),

            "deleted": True,
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# 成功済み単発タスク削除
# =========================================================

def delete_completed_tasks(
    db_config
):

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            DELETE FROM scheduled_tasks

            WHERE
                is_one_shot = 1
                AND status = %s
            """,
            (
                STATUS_SUCCESS,
            ),
        )

        deleted_count = (
            cursor.rowcount
        )

        conn.commit()

        return {
            "success": True,
            "deleted_count": (
                deleted_count
            ),
        }

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


# =========================================================
# failed一覧
# =========================================================

def list_failed_tasks(
    db_config
):

    conn = get_connection(
        db_config
    )

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT *
            FROM scheduled_tasks

            WHERE status = %s

            ORDER BY
                updated_at DESC,
                id DESC
            """,
            (
                STATUS_FAILED,
            ),
        )

        rows = (
            cursor.fetchall()
        )

        return [
            serialize_task_row(
                row
            )
            for row in rows
        ]

    finally:

        cursor.close()
        conn.close()