from pathlib import Path
import configparser
import mysql.connector
import uuid

DATABASE_CONFIG = Path("config/database.conf")


def _connect_db():
    config = configparser.ConfigParser()
    config.read(DATABASE_CONFIG, encoding="utf-8")

    dbconf = config["DATABASE"]

    return mysql.connector.connect(
        host=dbconf.get("host", "localhost"),
        port=dbconf.getint("port", 3306),
        user=dbconf.get("user", "Dail"),
        password=dbconf.get("password", ""),
        database=dbconf.get("database", "dail")
    )


def Create_session(process_uuid):
    #process_uuid = str(uuid.uuid4())

    conn = _connect_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS process_status (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            uuid CHAR(36) NOT NULL UNIQUE,
            finished TINYINT(1) NOT NULL DEFAULT 0
        )
        """
    )

    cursor.execute(
        """
        INSERT INTO process_status (uuid, finished)
        VALUES (%s, %s)
        """,
        (process_uuid, 0)
    )

    conn.commit()

    cursor.close()
    conn.close()



def End_session(process_uuid):
    conn = _connect_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE process_status
        SET finished = 1
        WHERE uuid = %s
        """,
        (process_uuid,)
    )

    conn.commit()

    cursor.close()
    conn.close()


def Check_previous_session():
    conn = _connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT finished
            FROM process_status
            ORDER BY id DESC
            LIMIT 1
            """
        )
    except mysql.connector.errors.ProgrammingError:
        # If the table does not exist, return 0 (no previous session)
        return 0

    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if row is None:
        return 0

    return 1 if row[0] == 0 else 0