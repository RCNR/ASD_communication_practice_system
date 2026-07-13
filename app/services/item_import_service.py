from __future__ import annotations

import csv
import io

from openpyxl import load_workbook
from sqlalchemy.orm import Session as DbSession

from app.models.item import Item

REQUIRED_COLUMNS = ["item_id", "use_type", "item_text"]
OPTIONAL_COLUMNS = [
    "ssis_domain",
    "situation_type",
    "sub_response_type",
    "difficulty",
    "primary_dv",
    "emotion_tag",
    "target_response",
    "hint_template",
    "example_score_2",
    "example_score_1",
    "example_score_0",
    "scoring_criteria",
    "cvi_score",
    "status",
]


def _parse_csv(content: bytes) -> list[dict]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp949")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def _parse_xlsx(content: bytes) -> list[dict]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    header = [str(cell).strip() if cell is not None else "" for cell in next(rows)]
    result = []
    for row in rows:
        if all(cell is None for cell in row):
            continue
        result.append({header[i]: row[i] for i in range(len(header)) if i < len(row)})
    return result


def parse_item_file(filename: str, content: bytes) -> list[dict]:
    if filename.lower().endswith(".xlsx"):
        return _parse_xlsx(content)
    return _parse_csv(content)


def upsert_items(db: DbSession, rows: list[dict]) -> tuple[int, list[str]]:
    upserted = 0
    errors = []

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

        missing = [col for col in REQUIRED_COLUMNS if not row.get(col)]
        if missing:
            errors.append(f"{i}행: 필수 컬럼 누락 ({', '.join(missing)})")
            continue

        cvi_score = row.get("cvi_score")
        try:
            cvi_score = float(cvi_score) if cvi_score not in (None, "") else None
        except ValueError:
            errors.append(f"{i}행: cvi_score 숫자 변환 실패 ({cvi_score})")
            continue

        db.merge(
            Item(
                item_id=row["item_id"],
                use_type=row["use_type"],
                ssis_domain=row.get("ssis_domain") or None,
                situation_type=row.get("situation_type") or None,
                sub_response_type=row.get("sub_response_type") or None,
                difficulty=row.get("difficulty") or None,
                primary_dv=row.get("primary_dv") or None,
                emotion_tag=row.get("emotion_tag") or None,
                item_text=row["item_text"],
                target_response=row.get("target_response") or None,
                hint_template=row.get("hint_template") or None,
                example_score_2=row.get("example_score_2") or None,
                example_score_1=row.get("example_score_1") or None,
                example_score_0=row.get("example_score_0") or None,
                scoring_criteria=row.get("scoring_criteria") or None,
                cvi_score=cvi_score,
                status=row.get("status") or "approved",
            )
        )
        upserted += 1

    db.commit()
    return upserted, errors
