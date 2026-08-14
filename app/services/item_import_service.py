from __future__ import annotations

import csv
import io

from openpyxl import load_workbook
from sqlalchemy.orm import Session as DbSession

from app.models.item import Item

REQUIRED_COLUMNS = ["item_id", "use_type", "set_no", "set_order", "item_text"]
OPTIONAL_COLUMNS = [
    "sentiment",
    "example_score_2",
    "example_score_1_ack",
    "example_score_1_con",
    "example_score_0",
    "hint_ack",
    "hint_con",
    "hint_fallback",
]

# 본 회기용 / 사전교육용. 업로드 화면마다 이 중 자기 쪽 값만 받도록 upsert_items에
# allowed_use_types로 넘긴다 (본 문항 xlsx를 사전교육 화면에 올리는 실수 방지).
SESSION_USE_TYPES = ("assessment", "intervention")
PRACTICE_USE_TYPES = ("practice_assessment", "practice_intervention")


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


def _parse_int(value) -> int | None:
    """xlsx gives numeric cells as int/float, csv gives strings - accept both,
    and reject anything that isn't a whole number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def upsert_items(
    db: DbSession, rows: list[dict], allowed_use_types: tuple[str, ...]
) -> tuple[int, list[str]]:
    upserted = 0
    errors = []
    seen_keys: dict[tuple[str, int, int], str] = {}

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

        missing = [col for col in REQUIRED_COLUMNS if row.get(col) in (None, "")]
        if missing:
            errors.append(f"{i}행: 필수 컬럼 누락 ({', '.join(missing)})")
            continue

        use_type = str(row["use_type"]).strip()
        if use_type not in allowed_use_types:
            errors.append(
                f"{i}행: 이 화면에서는 use_type이 {'/'.join(allowed_use_types)} 인 문항만 올릴 수 있습니다 (받은 값: {use_type})"
            )
            continue

        set_no = _parse_int(row["set_no"])
        set_order = _parse_int(row["set_order"])
        if set_no is None or set_order is None:
            errors.append(f"{i}행: set_no, set_order는 정수여야 합니다")
            continue

        # A duplicated (use_type, set_no, set_order) means two items compete for
        # the same slot in a session. Order within the set would then depend on
        # the DB's tiebreak, so flag it instead of importing silently.
        key = (use_type, set_no, set_order)
        if key in seen_keys:
            errors.append(f"{i}행: set_no {set_no}의 {set_order}번 자리가 {seen_keys[key]}와 중복됩니다")
            continue

        item_id = str(row["item_id"])
        seen_keys[key] = item_id

        # item_id is the table's primary key and is shared across every
        # use_type, so an id that already belongs to the other upload's group
        # would be silently overwritten by db.merge below. Refuse instead.
        existing = db.get(Item, item_id)
        if existing is not None and existing.use_type not in allowed_use_types:
            errors.append(f"{i}행: item_id {item_id}는 이미 {existing.use_type} 문항으로 등록되어 있습니다")
            continue

        db.merge(
            Item(
                item_id=item_id,
                use_type=use_type,
                set_no=set_no,
                set_order=set_order,
                sentiment=row.get("sentiment") or None,
                item_text=row["item_text"],
                example_score_2=row.get("example_score_2") or None,
                example_score_1_ack=row.get("example_score_1_ack") or None,
                example_score_1_con=row.get("example_score_1_con") or None,
                example_score_0=row.get("example_score_0") or None,
                # 힌트 재료는 중재용 문항에만 값이 있고, 평가용은 비어 있는 것이 정상이다.
                hint_ack=row.get("hint_ack") or None,
                hint_con=row.get("hint_con") or None,
                hint_fallback=row.get("hint_fallback") or None,
                status="approved",
            )
        )
        upserted += 1

    db.commit()
    return upserted, errors
