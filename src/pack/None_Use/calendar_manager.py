import csv
from datetime import date, datetime
from typing import Optional

import mysql.connector
from mysql.connector import Error


class CalendarManager:
    """
    Avelia Calendar Manager

    calendar_once
        単発予定

    calendar_weekly
        定期予定本体

    calendar_weekly_days
        定期予定の曜日

    calendar_weekly_status
        定期予定の日別完了状態
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        database: str,
        port: int = 3306,
    ):
        self.db_config = {
            "host": host,
            "user": user,
            "password": password,
            "database": database,
            "port": port,
            "autocommit": False,
        }

        self.create_tables()

    # =========================================================
    # DB
    # =========================================================

    def get_connection(self):
        return mysql.connector.connect(**self.db_config)

    def create_tables(self):
        """
        必要なテーブルを自動作成する。
        """

        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS calendar_once (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

                title VARCHAR(255) NOT NULL,
                description TEXT NULL,

                scheduled_date DATE NOT NULL,
                start_time TIME NULL,
                end_time TIME NULL,

                completed TINYINT(1) NOT NULL DEFAULT 0,
                completed_at DATETIME NULL,

                source VARCHAR(32) NOT NULL DEFAULT 'manual',

                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                INDEX idx_calendar_once_date (
                    scheduled_date,
                    completed
                )
            )
            """,

            """
            CREATE TABLE IF NOT EXISTS calendar_weekly (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

                title VARCHAR(255) NOT NULL,
                description TEXT NULL,

                start_time TIME NULL,
                end_time TIME NULL,

                enabled TINYINT(1) NOT NULL DEFAULT 1,

                source VARCHAR(32) NOT NULL DEFAULT 'manual',

                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            )
            """,

            """
            CREATE TABLE IF NOT EXISTS calendar_weekly_days (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

                schedule_id BIGINT UNSIGNED NOT NULL,
                weekday TINYINT UNSIGNED NOT NULL,

                UNIQUE KEY uq_weekly_day (
                    schedule_id,
                    weekday
                ),

                INDEX idx_weekday (
                    weekday
                ),

                CONSTRAINT fk_weekly_days_schedule
                    FOREIGN KEY (schedule_id)
                    REFERENCES calendar_weekly(id)
                    ON DELETE CASCADE
            )
            """,

            """
            CREATE TABLE IF NOT EXISTS calendar_weekly_status (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

                schedule_id BIGINT UNSIGNED NOT NULL,
                scheduled_date DATE NOT NULL,

                completed TINYINT(1) NOT NULL DEFAULT 0,
                completed_at DATETIME NULL,

                UNIQUE KEY uq_weekly_status (
                    schedule_id,
                    scheduled_date
                ),

                INDEX idx_weekly_status_date (
                    scheduled_date,
                    completed
                ),

                CONSTRAINT fk_weekly_status_schedule
                    FOREIGN KEY (schedule_id)
                    REFERENCES calendar_weekly(id)
                    ON DELETE CASCADE
            )
            """
        ]

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            for sql in sql_statements:
                cursor.execute(sql)

            conn.commit()

        except Error:
            conn.rollback()
            raise

        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # 単発予定
    # =========================================================

    def add_once(
        self,
        title: str,
        scheduled_date: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        source: str = "manual",
    ):
        sql = """
        INSERT INTO calendar_once (
            title,
            description,
            scheduled_date,
            start_time,
            end_time,
            source
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                sql,
                (
                    title,
                    description,
                    scheduled_date,
                    start_time,
                    end_time,
                    source,
                ),
            )

            schedule_id = cursor.lastrowid
            conn.commit()

            return {
                "success": True,
                "type": "once",
                "id": schedule_id,
                "message": "単発予定を登録しました。",
            }

        except Error as e:
            conn.rollback()
            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    def complete_once(self, schedule_id: int):
        sql = """
        UPDATE calendar_once
        SET
            completed = 1,
            completed_at = NOW()
        WHERE id = %s
          AND completed = 0
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(sql, (schedule_id,))
            affected = cursor.rowcount
            conn.commit()

            if affected == 0:
                return {
                    "success": False,
                    "message": "対象予定が存在しないか、すでに完了しています。",
                }

            return {
                "success": True,
                "id": schedule_id,
                "message": "予定を完了にしました。",
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    def delete_once(self, schedule_id: int):
        sql = """
        DELETE FROM calendar_once
        WHERE id = %s
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(sql, (schedule_id,))
            affected = cursor.rowcount
            conn.commit()

            return {
                "success": affected > 0,
                "deleted": affected,
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # 曜日指定 定期予定
    # =========================================================

    def add_weekly(
        self,
        title: str,
        weekdays: list[int],
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        source: str = "manual",
    ):
        """
        weekday:
        0 = 月
        1 = 火
        2 = 水
        3 = 木
        4 = 金
        5 = 土
        6 = 日
        """

        weekdays = sorted(set(int(day) for day in weekdays))

        for weekday in weekdays:
            if weekday < 0 or weekday > 6:
                return {
                    "success": False,
                    "error": f"不正な曜日番号です: {weekday}",
                }

        if not weekdays:
            return {
                "success": False,
                "error": "曜日が指定されていません。",
            }

        insert_schedule = """
        INSERT INTO calendar_weekly (
            title,
            description,
            start_time,
            end_time,
            source
        )
        VALUES (%s, %s, %s, %s, %s)
        """

        insert_day = """
        INSERT INTO calendar_weekly_days (
            schedule_id,
            weekday
        )
        VALUES (%s, %s)
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                insert_schedule,
                (
                    title,
                    description,
                    start_time,
                    end_time,
                    source,
                ),
            )

            schedule_id = cursor.lastrowid

            for weekday in weekdays:
                cursor.execute(
                    insert_day,
                    (
                        schedule_id,
                        weekday,
                    ),
                )

            conn.commit()

            return {
                "success": True,
                "type": "weekly",
                "id": schedule_id,
                "weekdays": weekdays,
                "message": "曜日指定予定を登録しました。",
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    def set_weekly_enabled(
        self,
        schedule_id: int,
        enabled: bool,
    ):
        sql = """
        UPDATE calendar_weekly
        SET enabled = %s
        WHERE id = %s
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                sql,
                (
                    int(enabled),
                    schedule_id,
                ),
            )

            affected = cursor.rowcount

            conn.commit()

            return {
                "success": affected > 0,
                "id": schedule_id,
                "enabled": bool(enabled),
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    def delete_weekly(self, schedule_id: int):
        sql = """
        DELETE FROM calendar_weekly
        WHERE id = %s
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(sql, (schedule_id,))
            affected = cursor.rowcount

            conn.commit()

            return {
                "success": affected > 0,
                "deleted": affected,
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # 定期予定 完了
    # =========================================================

    def complete_weekly(
        self,
        schedule_id: int,
        scheduled_date: Optional[str] = None,
    ):
        if scheduled_date is None:
            scheduled_date = date.today().isoformat()

        sql = """
        INSERT INTO calendar_weekly_status (
            schedule_id,
            scheduled_date,
            completed,
            completed_at
        )
        VALUES (%s, %s, 1, NOW())

        ON DUPLICATE KEY UPDATE
            completed = 1,
            completed_at = NOW()
        """

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                sql,
                (
                    schedule_id,
                    scheduled_date,
                ),
            )

            conn.commit()

            return {
                "success": True,
                "id": schedule_id,
                "scheduled_date": scheduled_date,
                "message": "定期予定を完了にしました。",
            }

        except Error as e:
            conn.rollback()

            return {
                "success": False,
                "error": str(e),
            }

        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # 予定取得
    # =========================================================

    def get_date_schedules(
        self,
        target_date: Optional[str] = None,
        unfinished_only: bool = True,
    ):
        if target_date is None:
            target = date.today()
        else:
            target = datetime.strptime(
                target_date,
                "%Y-%m-%d",
            ).date()

        weekday = target.weekday()

        conn = self.get_connection()

        try:
            # -----------------------------
            # 単発予定
            # -----------------------------

            once_cursor = conn.cursor(dictionary=True)

            once_sql = """
            SELECT
                id,
                title,
                description,
                scheduled_date,
                start_time,
                end_time,
                completed,
                completed_at,
                source
            FROM calendar_once
            WHERE scheduled_date = %s
            """

            params = [target]

            if unfinished_only:
                once_sql += " AND completed = 0"

            once_sql += """
            ORDER BY
                start_time IS NULL,
                start_time,
                id
            """

            once_cursor.execute(
                once_sql,
                params,
            )

            once_rows = once_cursor.fetchall()

            once_cursor.close()

            # -----------------------------
            # 曜日指定予定
            # -----------------------------

            weekly_cursor = conn.cursor(dictionary=True)

            weekly_sql = """
            SELECT
                cw.id,
                cw.title,
                cw.description,
                cw.start_time,
                cw.end_time,
                cw.source,

                COALESCE(
                    cws.completed,
                    0
                ) AS completed,

                cws.completed_at

            FROM calendar_weekly AS cw

            INNER JOIN calendar_weekly_days AS cwd
                ON cw.id = cwd.schedule_id

            LEFT JOIN calendar_weekly_status AS cws
                ON cw.id = cws.schedule_id
                AND cws.scheduled_date = %s

            WHERE
                cw.enabled = 1
                AND cwd.weekday = %s
            """

            if unfinished_only:
                weekly_sql += """
                AND COALESCE(
                    cws.completed,
                    0
                ) = 0
                """

            weekly_sql += """
            ORDER BY
                cw.start_time IS NULL,
                cw.start_time,
                cw.id
            """

            weekly_cursor.execute(
                weekly_sql,
                (
                    target,
                    weekday,
                ),
            )

            weekly_rows = weekly_cursor.fetchall()

            weekly_cursor.close()

            # -----------------------------
            # JSON化しやすい形へ
            # -----------------------------

            results = []

            for row in once_rows:
                results.append(
                    {
                        "schedule_type": "once",
                        "id": row["id"],
                        "title": row["title"],
                        "description": row["description"],
                        "date": target.isoformat(),
                        "start_time": self._time_to_string(
                            row["start_time"]
                        ),
                        "end_time": self._time_to_string(
                            row["end_time"]
                        ),
                        "completed": bool(
                            row["completed"]
                        ),
                        "source": row["source"],
                    }
                )

            for row in weekly_rows:
                results.append(
                    {
                        "schedule_type": "weekly",
                        "id": row["id"],
                        "title": row["title"],
                        "description": row["description"],
                        "date": target.isoformat(),
                        "start_time": self._time_to_string(
                            row["start_time"]
                        ),
                        "end_time": self._time_to_string(
                            row["end_time"]
                        ),
                        "completed": bool(
                            row["completed"]
                        ),
                        "source": row["source"],
                    }
                )

            results.sort(
                key=lambda item: (
                    item["start_time"] is None,
                    item["start_time"] or "",
                    item["id"],
                )
            )

            return {
                "success": True,
                "date": target.isoformat(),
                "weekday": weekday,
                "count": len(results),
                "schedules": results,
            }

        except Error as e:
            return {
                "success": False,
                "error": str(e),
            }

        finally:
            conn.close()

    def get_today_schedules(
        self,
        unfinished_only: bool = True,
    ):
        return self.get_date_schedules(
            target_date=date.today().isoformat(),
            unfinished_only=unfinished_only,
        )

    # =========================================================
    # 一覧
    # =========================================================

    def list_once(self):
        sql = """
        SELECT
            id,
            title,
            description,
            scheduled_date,
            start_time,
            end_time,
            completed,
            source
        FROM calendar_once
        ORDER BY
            scheduled_date,
            start_time IS NULL,
            start_time
        """

        conn = self.get_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute(sql)

            rows = cursor.fetchall()

            for row in rows:
                row["scheduled_date"] = str(
                    row["scheduled_date"]
                )
                row["start_time"] = self._time_to_string(
                    row["start_time"]
                )
                row["end_time"] = self._time_to_string(
                    row["end_time"]
                )
                row["completed"] = bool(
                    row["completed"]
                )

            return {
                "success": True,
                "count": len(rows),
                "schedules": rows,
            }

        finally:
            cursor.close()
            conn.close()

    def list_weekly(self):
        sql = """
        SELECT
            cw.id,
            cw.title,
            cw.description,
            cw.start_time,
            cw.end_time,
            cw.enabled,
            cw.source,
            GROUP_CONCAT(
                cwd.weekday
                ORDER BY cwd.weekday
            ) AS weekdays

        FROM calendar_weekly AS cw

        LEFT JOIN calendar_weekly_days AS cwd
            ON cw.id = cwd.schedule_id

        GROUP BY
            cw.id,
            cw.title,
            cw.description,
            cw.start_time,
            cw.end_time,
            cw.enabled,
            cw.source

        ORDER BY
            cw.start_time IS NULL,
            cw.start_time,
            cw.id
        """

        conn = self.get_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute(sql)

            rows = cursor.fetchall()

            for row in rows:
                row["start_time"] = self._time_to_string(
                    row["start_time"]
                )

                row["end_time"] = self._time_to_string(
                    row["end_time"]
                )

                row["enabled"] = bool(
                    row["enabled"]
                )

                if row["weekdays"]:
                    row["weekdays"] = [
                        int(x)
                        for x
                        in row["weekdays"].split(",")
                    ]
                else:
                    row["weekdays"] = []

            return {
                "success": True,
                "count": len(rows),
                "schedules": rows,
            }

        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # CSV
    # =========================================================

    def import_csv(
        self,
        csv_path: str,
    ):
        """
        CSV type:
        once
        weekly

        once:
        type,title,description,date,start_time,end_time

        weekly:
        type,title,description,weekdays,start_time,end_time

        weekdays例:
        1|2|4|5
        """

        added = 0
        skipped = 0
        errors = []

        with open(
            csv_path,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            reader = csv.DictReader(f)

            for row_number, row in enumerate(
                reader,
                start=2,
            ):
                try:
                    schedule_type = (
                        row.get("type", "")
                        .strip()
                        .lower()
                    )

                    title = (
                        row.get("title", "")
                        .strip()
                    )

                    if not title:
                        raise ValueError(
                            "titleが空です"
                        )

                    description = (
                        row.get(
                            "description",
                            "",
                        ).strip()
                        or None
                    )

                    start_time = (
                        row.get(
                            "start_time",
                            "",
                        ).strip()
                        or None
                    )

                    end_time = (
                        row.get(
                            "end_time",
                            "",
                        ).strip()
                        or None
                    )

                    if schedule_type == "once":

                        scheduled_date = (
                            row.get(
                                "date",
                                "",
                            ).strip()
                        )

                        if not scheduled_date:
                            raise ValueError(
                                "dateが空です"
                            )

                        result = self.add_once(
                            title=title,
                            description=description,
                            scheduled_date=scheduled_date,
                            start_time=start_time,
                            end_time=end_time,
                            source="csv",
                        )

                    elif schedule_type == "weekly":

                        weekday_text = (
                            row.get(
                                "weekdays",
                                "",
                            ).strip()
                        )

                        if not weekday_text:
                            raise ValueError(
                                "weekdaysが空です"
                            )

                        weekdays = [
                            int(x)
                            for x
                            in weekday_text.split("|")
                        ]

                        result = self.add_weekly(
                            title=title,
                            description=description,
                            weekdays=weekdays,
                            start_time=start_time,
                            end_time=end_time,
                            source="csv",
                        )

                    else:
                        raise ValueError(
                            f"未対応type: {schedule_type}"
                        )

                    if result.get("success"):
                        added += 1
                    else:
                        skipped += 1

                        errors.append(
                            {
                                "row": row_number,
                                "error": result.get(
                                    "error",
                                    "登録失敗",
                                ),
                            }
                        )

                except Exception as e:
                    skipped += 1

                    errors.append(
                        {
                            "row": row_number,
                            "error": str(e),
                        }
                    )

        return {
            "success": True,
            "added": added,
            "skipped": skipped,
            "errors": errors,
        }

    # =========================================================
    # Utility
    # =========================================================

    @staticmethod
    def _time_to_string(value):
        if value is None:
            return None

        if hasattr(value, "total_seconds"):
            seconds = int(
                value.total_seconds()
            )

            hours = seconds // 3600

            minutes = (
                seconds % 3600
            ) // 60

            seconds = seconds % 60

            return (
                f"{hours:02d}:"
                f"{minutes:02d}:"
                f"{seconds:02d}"
            )

        return str(value)