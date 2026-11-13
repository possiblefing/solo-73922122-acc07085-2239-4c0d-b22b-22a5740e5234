"""CSV 上传解析与校验。

每份 CSV 须含表头 id,date,currency,amount_cents（大小写不敏感，允许多余列），
数据行 1..100 行，id 在本侧文件内唯一，金额为正整数分。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

MAX_ROWS = 100
MAX_UPLOAD_BYTES = 256 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
_AMOUNT_RE = re.compile(r"^\d{1,15}$")

REQUIRED_COLUMNS = ("id", "date", "currency", "amount_cents")


class CsvError(ValueError):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


def parse_csv_text(text: str, side: str) -> list[dict]:
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        raise CsvError([f"{side}: 文件为空，缺少表头"])

    header = [h.strip().lower() for h in raw_header]
    col_idx: dict[str, int] = {}
    for col in REQUIRED_COLUMNS:
        if col in header:
            col_idx[col] = header.index(col)
        else:
            errors.append(f"{side}: 缺少必需列 {col!r}（表头须含 {','.join(REQUIRED_COLUMNS)}）")
    if errors:
        raise CsvError(errors)

    rows: list[dict] = []
    seen_ids: set[str] = set()
    for line_no, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue  # 跳过空行
        if len(rows) >= MAX_ROWS:
            errors.append(f"{side}: 数据行超过上限 {MAX_ROWS} 行")
            break
        if len(record) < len(header):
            errors.append(f"{side}: 第 {line_no} 行列数不足")
            continue

        def cell(col: str) -> str:
            return record[col_idx[col]].strip()

        row_id = cell("id")
        rdate = cell("date")
        currency = cell("currency")
        amount = cell("amount_cents")

        if not _ID_RE.match(row_id):
            errors.append(
                f"{side}: 第 {line_no} 行 id {row_id!r} 非法"
                "（须为 1-64 位字母数字开头，可含 _ . -）"
            )
        elif row_id in seen_ids:
            errors.append(f"{side}: 第 {line_no} 行 id {row_id!r} 在本侧文件内重复")
        if not _DATE_RE.match(rdate):
            errors.append(f"{side}: 第 {line_no} 行 date {rdate!r} 非法（须为 YYYY-MM-DD）")
        else:
            try:
                datetime.strptime(rdate, "%Y-%m-%d")
            except ValueError:
                errors.append(f"{side}: 第 {line_no} 行 date {rdate!r} 不是有效日历日期")
        if not _CURRENCY_RE.match(currency):
            errors.append(f"{side}: 第 {line_no} 行 currency {currency!r} 非法（须为 3 位字母币种代码）")
        if not _AMOUNT_RE.match(amount) or int(amount or 0) <= 0:
            errors.append(f"{side}: 第 {line_no} 行 amount_cents {amount!r} 非法（须为正整数分）")

        if row_id and row_id not in seen_ids:
            seen_ids.add(row_id)
        rows.append(
            {
                "row_id": row_id,
                "rdate": rdate,
                "currency": currency.upper(),
                "amount_cents": int(amount) if _AMOUNT_RE.match(amount) else 0,
            }
        )

    if not rows and not errors:
        errors.append(f"{side}: 没有数据行（须含 1-{MAX_ROWS} 行）")
    if errors:
        raise CsvError(errors)
    return rows


def parse_upload(data: bytes, side: str) -> list[dict]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise CsvError([f"{side}: 文件超过大小上限 {MAX_UPLOAD_BYTES // 1024}KB"])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError([f"{side}: 文件须为 UTF-8 编码"])
    return parse_csv_text(text, side)
