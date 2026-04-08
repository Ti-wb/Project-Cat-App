from pydantic import BaseModel
from typing import Optional, List


class CatBrief(BaseModel):
    animal_id: int
    animal_subid: Optional[str] = None
    animal_variety: Optional[str] = None
    animal_sex: Optional[str] = None
    animal_bodytype: Optional[str] = None
    animal_colour: Optional[str] = None
    animal_age: Optional[str] = None
    animal_sterilization: Optional[str] = None
    animal_status: Optional[str] = None
    shelter_name: Optional[str] = None
    area_pkid: Optional[int] = None
    image_url: Optional[str] = None


class CatDetail(BaseModel):
    animal_id: int
    animal_subid: Optional[str] = None
    animal_place: Optional[str] = None
    animal_variety: Optional[str] = None
    animal_sex: Optional[str] = None
    animal_bodytype: Optional[str] = None
    animal_colour: Optional[str] = None
    animal_age: Optional[str] = None
    animal_sterilization: Optional[str] = None
    animal_bacterin: Optional[str] = None
    animal_foundplace: Optional[str] = None
    animal_status: Optional[str] = None
    animal_remark: Optional[str] = None
    animal_opendate: Optional[str] = None
    animal_closeddate: Optional[str] = None
    animal_update: Optional[str] = None
    animal_createtime: Optional[str] = None
    shelter_name: Optional[str] = None
    shelter_address: Optional[str] = None
    shelter_tel: Optional[str] = None
    album_file: Optional[str] = None
    local_image: Optional[str] = None
    area_pkid: Optional[int] = None
    shelter_pkid: Optional[int] = None
    synced_at: Optional[str] = None
    image_url: Optional[str] = None


class ShelterInfo(BaseModel):
    shelter_name: Optional[str] = None
    shelter_address: Optional[str] = None
    shelter_tel: Optional[str] = None
    area_pkid: Optional[int] = None
    count: int


class CatListResponse(BaseModel):
    total: int
    items: List[CatBrief]
    offset: int
    limit: int
