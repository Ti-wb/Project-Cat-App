"""
main.py - FastAPI backend for Taiwan public shelter cat adoption app.
"""
import os
import sqlite3
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from database import get_connection, init_db
from models import CatBrief, CatDetail, ShelterInfo, CatListResponse

app = FastAPI(title="Cat Adoption API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


def _image_url(animal_id: int, local_image: Optional[str]) -> Optional[str]:
    if local_image:
        return f"/images/{animal_id}.png"
    return None


def _row_to_brief(row: sqlite3.Row) -> CatBrief:
    d = dict(row)
    d["image_url"] = _image_url(d["animal_id"], d.get("local_image"))
    return CatBrief(**d)


def _row_to_detail(row: sqlite3.Row) -> CatDetail:
    d = dict(row)
    d["image_url"] = _image_url(d["animal_id"], d.get("local_image"))
    return CatDetail(**d)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM cats").fetchone()[0]
    conn.close()
    return {"status": "ok", "cats_count": count}


@app.get("/api/cats", response_model=CatListResponse)
def list_cats(
    shelter: Optional[str] = Query(None),
    area_pkid: Optional[int] = Query(None),
    colour: Optional[str] = Query(None),
    age: Optional[str] = Query(None),
    sex: Optional[str] = Query(None),
    bodytype: Optional[str] = Query(None),
    sterilization: Optional[str] = Query(None),
    status: Optional[str] = Query("OPEN"),
    q: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    conn = get_connection()

    conditions: list[str] = []
    params: list = []

    if shelter:
        conditions.append("shelter_name = ?")
        params.append(shelter)
    if area_pkid is not None:
        conditions.append("area_pkid = ?")
        params.append(area_pkid)
    if colour:
        conditions.append("animal_colour = ?")
        params.append(colour)
    if age:
        conditions.append("animal_age = ?")
        params.append(age)
    if sex:
        conditions.append("animal_sex = ?")
        params.append(sex)
    if bodytype:
        conditions.append("animal_bodytype = ?")
        params.append(bodytype)
    if sterilization:
        conditions.append("animal_sterilization = ?")
        params.append(sterilization)
    if status:
        conditions.append("animal_status = ?")
        params.append(status)
    if q:
        conditions.append(
            "(animal_variety LIKE ? OR animal_foundplace LIKE ? OR animal_remark LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total_row = conn.execute(f"SELECT COUNT(*) FROM cats {where}", params).fetchone()
    total = total_row[0]

    rows = conn.execute(
        f"SELECT * FROM cats {where} ORDER BY animal_id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()

    items = [_row_to_brief(r) for r in rows]
    return CatListResponse(total=total, items=items, offset=offset, limit=limit)


@app.get("/api/cats/{animal_id}", response_model=CatDetail)
def get_cat(animal_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM cats WHERE animal_id = ?", (animal_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Cat not found")
    return _row_to_detail(row)


@app.get("/api/shelters", response_model=List[ShelterInfo])
def list_shelters():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT shelter_name, shelter_address, shelter_tel, area_pkid,
               COUNT(*) as count
        FROM cats
        GROUP BY shelter_name, shelter_address, shelter_tel, area_pkid
        ORDER BY shelter_name
        """
    ).fetchall()
    conn.close()
    return [ShelterInfo(**dict(r)) for r in rows]


@app.get("/api/filters")
def get_filters():
    conn = get_connection()

    def distinct(col: str) -> list[str]:
        rows = conn.execute(
            f"SELECT DISTINCT {col} FROM cats WHERE {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
        ).fetchall()
        return [r[0] for r in rows]

    result = {
        "colours": distinct("animal_colour"),
        "ages": distinct("animal_age"),
        "sexes": distinct("animal_sex"),
        "bodytypes": distinct("animal_bodytype"),
        "sterilizations": distinct("animal_sterilization"),
    }
    conn.close()
    return result
