from fastapi import APIRouter

from pydantic import BaseModel


class Contributor(BaseModel):
    nickname: str
    picture_url: str
    github_url: str


contributor_list : list[Contributor] = [Contributor(
    nickname="dethrandir",
    picture_url="https://github.com/dethrandir.png",
    github_url="https://github.com/dethrandir",
),
Contributor(
    nickname="Burakkandemir10",
    picture_url="https://github.com/Burakkandemir10.png",
    github_url="https://github.com/Burakkandemir10",
),
Contributor(
    nickname="canaltngyk",
    picture_url="https://github.com/canaltngyk.png",
    github_url="https://github.com/canaltngyk",
),
Contributor(
    nickname="burockkkk",
    picture_url="https://github.com/burockkkk.png",
    github_url="https://github.com/burockkkk",
)]

router = APIRouter()


@router.get("/contributors")
async def get_contributors():
    return {"contributors": contributor_list}
