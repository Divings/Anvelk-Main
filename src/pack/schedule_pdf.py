import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import mysql.connector

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
import configparser

# ============================================================
# 設定
# ============================================================

ANVELK_DB_CONF = Path(
    "/opt/Anvelk-Mainframe/config/database.conf"
)

DEFAULT_OUTPUT_DIR =  Path.home() / "out_pdf"

FONT_NORMAL = "HeiseiMin-W3"
FONT_BOLD = "HeiseiKakuGo-W5"


# ============================================================
# 日本語フォント
# ============================================================

pdfmetrics.registerFont(
    UnicodeCIDFont(FONT_NORMAL)
)

pdfmetrics.registerFont(
    UnicodeCIDFont(FONT_BOLD)
)


# ============================================================
# 例外
# ============================================================

class PDFToolError(Exception):
    pass


class ScheduleDBError(Exception):
    pass


# ============================================================
# DB
# ============================================================

def _load_db_config():
    """
    Anvelk Mainframeのdatabase.confから
    DB接続設定を読み込む。
    """

    if not ANVELK_DB_CONF.is_file():
        raise ScheduleDBError(
            f"DB設定ファイルが存在しません: {ANVELK_DB_CONF}"
        )

    config = configparser.ConfigParser()

    try:
        read_files = config.read(
            ANVELK_DB_CONF,
            encoding="utf-8"
        )

    except (OSError, configparser.Error) as e:
        raise ScheduleDBError(
            f"DB設定ファイルの読み込みに失敗しました: {e}"
        )

    if not read_files:
        raise ScheduleDBError(
            f"DB設定ファイルを読み込めませんでした: {ANVELK_DB_CONF}"
        )

    if "DATABASE" not in config:
        raise ScheduleDBError(
            "database.conf に [DATABASE] セクションがありません"
        )

    db = config["DATABASE"]

    required = (
        "host",
        "user",
        "password",
        "database",
    )

    missing = [
        key
        for key in required
        if key not in db
    ]

    if missing:
        raise ScheduleDBError(
            "DB設定が不足しています: "
            + ", ".join(missing)
        )

    result = {
        "host": db["host"],
        "user": db["user"],
        "password": db["password"],
        "database": db["database"],
    }

    # portが書いてあれば使用
    if db.get("port"):
        try:
            result["port"] = db.getint("port")
        except ValueError:
            raise ScheduleDBError(
                "database.conf の port が不正です"
            )

    return result

def _connect_db():
    """
    Anvelk Mainframeの設定を使用してMariaDB/MySQLへ接続。
    """

    db_config = _load_db_config()

    try:
        return mysql.connector.connect(
            **db_config
        )

    except mysql.connector.Error as e:
        raise ScheduleDBError(
            f"DB接続に失敗しました: {e}"
        )


# ============================================================
# 時刻変換
# ============================================================

def _format_time(value):
    """
    DBから取得した時刻をHH:MM形式へ変換
    """

    if value is None:
        return ""

    # MySQL TIMEがtimedeltaになる場合
    if isinstance(
        value,
        datetime.timedelta
    ):
        total_seconds = int(
            value.total_seconds()
        )

        hours = (
            total_seconds // 3600
        )

        minutes = (
            total_seconds % 3600
        ) // 60

        return (
            f"{hours:02d}:"
            f"{minutes:02d}"
        )

    # datetime.time
    if isinstance(
        value,
        datetime.time
    ):
        return value.strftime(
            "%H:%M"
        )

    text = str(value)

    # 13:00:00 → 13:00
    if len(text) >= 5:
        return text[:5]

    return text


# ============================================================
# 今日の予定取得
# ============================================================

def get_today_schedules():
    """
    calendar_once と calendar_weekly から
    今日の予定を取得する。
    """

    today = datetime.date.today()

    # calendar_weekly_days.weekday が
    # Pythonと同じ 月=0 ～ 日=6 で保存されている前提
    weekday = today.weekday()

    schedules = []

    conn = _connect_db()
    cursor = conn.cursor(dictionary=True)

    try:
        # ====================================================
        # 単発予定
        # ====================================================

        cursor.execute(
            """
            SELECT
                id,
                title,
                description,
                start_time,
                end_time,
                completed,
                source
            FROM calendar_once
            WHERE scheduled_date = %s
            ORDER BY
                start_time IS NULL,
                start_time,
                id
            """,
            (today,)
        )

        for row in cursor.fetchall():
            schedules.append({
                "id": row["id"],
                "source_type": "once",
                "source": row.get("source") or "",
                "start_time": _format_time(
                    row.get("start_time")
                ),
                "end_time": _format_time(
                    row.get("end_time")
                ),
                "title": row.get("title") or "",
                "description": row.get("description") or "",
                "completed": bool(
                    row.get("completed")
                ),
            })

        # ====================================================
        # 毎週予定
        # ====================================================

        cursor.execute(
            """
            SELECT
                w.id,
                w.title,
                w.description,
                w.start_time,
                w.end_time,
                w.source,
                COALESCE(s.completed, 0) AS completed
            FROM calendar_weekly AS w

            INNER JOIN calendar_weekly_days AS d
                ON d.schedule_id = w.id

            LEFT JOIN calendar_weekly_status AS s
                ON s.schedule_id = w.id
               AND s.scheduled_date = %s

            WHERE
                w.enabled = 1
                AND d.weekday = %s

            ORDER BY
                w.start_time IS NULL,
                w.start_time,
                w.id
            """,
            (
                today,
                weekday
            )
        )

        for row in cursor.fetchall():
            schedules.append({
                "id": row["id"],
                "source_type": "weekly",
                "source": row.get("source") or "",
                "start_time": _format_time(
                    row.get("start_time")
                ),
                "end_time": _format_time(
                    row.get("end_time")
                ),
                "title": row.get("title") or "",
                "description": row.get("description") or "",
                "completed": bool(
                    row.get("completed")
                ),
            })

    except mysql.connector.Error as e:
        raise ScheduleDBError(
            f"予定取得に失敗しました: {e}"
        )

    finally:
        cursor.close()
        conn.close()

    # ========================================================
    # 全予定を時刻順に統合
    # ========================================================

    schedules.sort(
        key=lambda x: (
            x["start_time"] == "",
            x["start_time"],
            x["title"]
        )
    )

    return schedules
    

# ============================================================
# PDF用スタイル
# ============================================================

def _create_styles():

    styles = getSampleStyleSheet()

    return {

        "title":
            ParagraphStyle(
                "AveliaTitle",
                parent=styles["Title"],
                fontName=FONT_BOLD,
                fontSize=20,
                leading=26,
                alignment=TA_CENTER,
                spaceAfter=8 * mm,
            ),

        "subtitle":
            ParagraphStyle(
                "AveliaSubtitle",
                parent=styles["Normal"],
                fontName=FONT_NORMAL,
                fontSize=10,
                leading=15,
                alignment=TA_CENTER,
                textColor=colors.grey,
                spaceAfter=7 * mm,
            ),

        "heading":
            ParagraphStyle(
                "AveliaHeading",
                parent=styles["Heading2"],
                fontName=FONT_BOLD,
                fontSize=14,
                leading=20,
                spaceBefore=4 * mm,
                spaceAfter=3 * mm,
            ),

        "paragraph":
            ParagraphStyle(
                "AveliaParagraph",
                parent=styles["BodyText"],
                fontName=FONT_NORMAL,
                fontSize=10.5,
                leading=17,
                alignment=TA_LEFT,
                spaceAfter=3 * mm,
            ),

        "list":
            ParagraphStyle(
                "AveliaList",
                parent=styles["BodyText"],
                fontName=FONT_NORMAL,
                fontSize=10.5,
                leading=17,
                leftIndent=6 * mm,
                firstLineIndent=-3 * mm,
                spaceAfter=1.5 * mm,
            ),

        "table":
            ParagraphStyle(
                "AveliaTable",
                parent=styles["BodyText"],
                fontName=FONT_NORMAL,
                fontSize=9,
                leading=13,
            ),

        "table_header":
            ParagraphStyle(
                "AveliaTableHeader",
                parent=styles["BodyText"],
                fontName=FONT_BOLD,
                fontSize=9,
                leading=13,
            ),
    }


# ============================================================
# HTMLエスケープ
# ============================================================

def _escape_text(value: Any):

    text = (
        ""
        if value is None
        else str(value)
    )

    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace(
            "\n",
            "<br/>"
        )
    )


# ============================================================
# 汎用PDF Generator
# ============================================================

class PDFGenerator:

    def __init__(
        self,
        output_path: str,
        page_size: str = "A4",
        author: str = "Avelia",
    ):

        self.output_path = Path(
            output_path
        )

        self.author = author

        if page_size.upper() == "A4":

            self.page_size = A4

        elif page_size.upper() in (
            "A4_LANDSCAPE",
            "LANDSCAPE"
        ):

            self.page_size = landscape(
                A4
            )

        else:

            raise PDFToolError(
                "未対応のページサイズ: "
                + page_size
            )

        self.styles = (
            _create_styles()
        )

        self.story = []

    # --------------------------------------------------------
    # タイトル
    # --------------------------------------------------------

    def add_title(
        self,
        text
    ):

        self.story.append(
            Paragraph(
                _escape_text(text),
                self.styles["title"]
            )
        )

    # --------------------------------------------------------
    # サブタイトル
    # --------------------------------------------------------

    def add_subtitle(
        self,
        text
    ):

        self.story.append(
            Paragraph(
                _escape_text(text),
                self.styles[
                    "subtitle"
                ]
            )
        )

    # --------------------------------------------------------
    # 見出し
    # --------------------------------------------------------

    def add_heading(
        self,
        text
    ):

        self.story.append(
            Paragraph(
                _escape_text(text),
                self.styles[
                    "heading"
                ]
            )
        )

    # --------------------------------------------------------
    # 段落
    # --------------------------------------------------------

    def add_paragraph(
        self,
        text
    ):

        self.story.append(
            Paragraph(
                _escape_text(text),
                self.styles[
                    "paragraph"
                ]
            )
        )

    # --------------------------------------------------------
    # リスト
    # --------------------------------------------------------

    def add_list(
        self,
        items
    ):

        for item in items:

            self.story.append(
                Paragraph(
                    "・"
                    + _escape_text(
                        item
                    ),
                    self.styles[
                        "list"
                    ]
                )
            )

    # --------------------------------------------------------
    # 空白
    # --------------------------------------------------------

    def add_spacer(
        self,
        height_mm=5
    ):

        self.story.append(
            Spacer(
                1,
                height_mm * mm
            )
        )

    # --------------------------------------------------------
    # 改ページ
    # --------------------------------------------------------

    def add_page_break(
        self
    ):

        self.story.append(
            PageBreak()
        )

    # --------------------------------------------------------
    # 表
    # --------------------------------------------------------

    def add_table(
        self,
        headers,
        rows,
        column_widths=None
    ):

        if not headers:

            raise PDFToolError(
                "tableにはheadersが必要です"
            )

        column_count = len(
            headers
        )

        for row in rows:

            if len(row) != column_count:

                raise PDFToolError(
                    "表の列数が一致していません"
                )

        table_data = []

        # ヘッダー

        table_data.append([
            Paragraph(
                _escape_text(cell),
                self.styles[
                    "table_header"
                ]
            )
            for cell in headers
        ])

        # データ

        for row in rows:

            table_data.append([
                Paragraph(
                    _escape_text(cell),
                    self.styles[
                        "table"
                    ]
                )
                for cell in row
            ])

        # 列幅

        if column_widths:

            widths = [
                width * mm
                for width
                in column_widths
            ]

        else:

            widths = None

        table = Table(
            table_data,
            colWidths=widths,
            repeatRows=1,
            hAlign="LEFT"
        )

        table.setStyle(
            TableStyle([

                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    colors.HexColor(
                        "#E8E8E8"
                    )
                ),

                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.5,
                    colors.HexColor(
                        "#AAAAAA"
                    )
                ),

                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "TOP"
                ),

                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    5
                ),

                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    5
                ),

                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    5
                ),

                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    5
                ),
            ])
        )

        self.story.append(
            table
        )

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------

    def save(self):

        self.output_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        document = SimpleDocTemplate(
            str(
                self.output_path
            ),

            pagesize=self.page_size,

            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,

            title=self.output_path.stem,
            author=self.author,
        )

        document.build(
            self.story
        )

        return str(
            self.output_path
        )


# ============================================================
# 汎用Document → PDF
# ============================================================

def create_pdf(
    document: Dict[str, Any],
    output_path: str
):

    generator = PDFGenerator(
        output_path=output_path,

        page_size=document.get(
            "page_size",
            "A4"
        ),

        author=document.get(
            "author",
            "Avelia"
        )
    )

    # タイトル

    if document.get("title"):

        generator.add_title(
            document["title"]
        )

    # サブタイトル

    if document.get("subtitle"):

        generator.add_subtitle(
            document["subtitle"]
        )

    # ブロック

    for block in document.get(
        "blocks",
        []
    ):

        block_type = block.get(
            "type"
        )

        if block_type == "heading":

            generator.add_heading(
                block.get(
                    "text",
                    ""
                )
            )

        elif block_type == "paragraph":

            generator.add_paragraph(
                block.get(
                    "text",
                    ""
                )
            )

        elif block_type == "list":

            generator.add_list(
                block.get(
                    "items",
                    []
                )
            )

        elif block_type == "table":

            generator.add_table(

                headers=block.get(
                    "headers",
                    []
                ),

                rows=block.get(
                    "rows",
                    []
                ),

                column_widths=block.get(
                    "column_widths"
                )
            )

        elif block_type == "spacer":

            generator.add_spacer(
                block.get(
                    "height_mm",
                    5
                )
            )

        elif block_type == "page_break":

            generator.add_page_break()

        else:

            raise PDFToolError(
                "未対応のblock type: "
                + str(block_type)
            )

    return generator.save()


# ============================================================
# 今日の予定 → Document
# ============================================================

def build_today_schedule_document():

    schedules = (
        get_today_schedules()
    )

    today = datetime.date.today()

    rows = []

    for schedule in schedules:

        start = schedule.get(
            "start_time",
            ""
        )

        end = schedule.get(
            "end_time",
            ""
        )

        if start and end:

            time_text = (
                f"{start} - {end}"
            )

        elif start:

            time_text = start

        else:

            time_text = ""

        rows.append([
            time_text,
            schedule.get(
                "title",
                ""
            )
        ])

    if not rows:

        rows.append([
            "",
            "本日の予定はありません。"
        ])

    return {

        "title":
            "本日の予定",

        "subtitle":
            today.strftime(
                "%Y年%m月%d日"
            ),

        "author":
            "Avelia",

        "blocks": [

            {
                "type":
                    "table",

                "headers": [
                    "時刻",
                    "予定"
                ],

                "rows":
                    rows,

                "column_widths": [
                    45,
                    125
                ]
            }
        ]
    }


# ============================================================
# 今日の予定PDF生成
# ============================================================

def create_today_schedule_pdf(
    output_path=None
):

    today = datetime.date.today()

    if output_path is None:

        filename = (
            "schedule_"
            + today.strftime(
                "%Y%m%d"
            )
            + ".pdf"
        )

        output_path = str(
            Path(
                DEFAULT_OUTPUT_DIR
            )
            / filename
        )

    document = (
        build_today_schedule_document()
    )

    return create_pdf(
        document=document,
        output_path=output_path
    )


# ============================================================
# テスト
# ============================================================

if __name__ == "__main__":

    try:

        output = (
            create_today_schedule_pdf()
        )

        print(
            "PDFを生成しました:"
        )

        print(output)

    except Exception as e:

        print(
            "PDF生成エラー:",
            e
        )
def tool_create_general_pdf(
    title,
    content,
    filename=None
):
    """
    Avelia Tool向け汎用PDF生成。
    """

    try:
        output_dir = Path(DEFAULT_OUTPUT_DIR)

        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        if filename:
            # ディレクトリを指定されてもファイル名部分だけ使用
            filename = Path(filename).name

            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"

        else:
            filename = (
                "document_"
                + datetime.datetime.now().strftime(
                    "%Y%m%d_%H%M%S"
                )
                + ".pdf"
            )

        output_path = output_dir / filename

        document = {
            "title": title,
            "author": "Avelia",
            "blocks": [
                {
                    "type": "paragraph",
                    "text": content
                }
            ]
        }

        path = create_pdf(
            document=document,
            output_path=str(output_path)
        )

        return {
            "success": True,
            "path": path,
            "message": (
                f"PDFを作成しました: {path}"
            )
        }

    except PDFToolError as e:
        return {
            "success": False,
            "path": None,
            "message": (
                f"PDFの生成に失敗しました: {e}"
            )
        }

    except Exception as e:
        return {
            "success": False,
            "path": None,
            "message": (
                "PDF生成中にエラーが発生しました: "
                f"{type(e).__name__}: {e}"
            )
        }
def tool_create_schedule_pdf(output_path=None):
    """
    Avelia Tool向けエントリーポイント。

    今日の予定をDBから取得してPDFを生成する。

    Returns:
        dict
    """

    try:
        path = create_today_schedule_pdf(
            output_path=output_path
        )

        return {
            "success": True,
            "path": path,
            "message": f"今日の予定をPDFに出力しました: {path}"
        }

    except ScheduleDBError as e:
        return {
            "success": False,
            "path": None,
            "message": f"予定データの取得に失敗しました: {e}"
        }

    except PDFToolError as e:
        return {
            "success": False,
            "path": None,
            "message": f"PDFの生成に失敗しました: {e}"
        }

    except Exception as e:
        return {
            "success": False,
            "path": None,
            "message": f"予定PDF生成中にエラーが発生しました: {e}"
        }