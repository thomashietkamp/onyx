"""Admin routes for standard answers and their categories. Shapes follow
`StandardAnswer` and `StandardAnswerCategory` in `web/src/lib/types.ts`."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.models import StandardAnswer, StandardAnswerCategory, User
from onyx.db.standard_answer import (
    deactivate_standard_answer,
    delete_standard_answer_category,
    fetch_standard_answer_categories,
    fetch_standard_answers,
    insert_standard_answer,
    insert_standard_answer_category,
    update_standard_answer,
    update_standard_answer_category,
)

router = APIRouter(prefix="/manage/admin/standard-answer")

_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)


class StandardAnswerCategorySnapshot(BaseModel):
    id: int
    name: str

    @classmethod
    def from_model(
        cls, category: StandardAnswerCategory
    ) -> "StandardAnswerCategorySnapshot":
        return cls(id=category.id, name=category.name)


class StandardAnswerSnapshot(BaseModel):
    id: int
    keyword: str
    answer: str
    match_regex: bool
    match_any_keywords: bool
    categories: list[StandardAnswerCategorySnapshot]

    @classmethod
    def from_model(cls, answer: StandardAnswer) -> "StandardAnswerSnapshot":
        return cls(
            id=answer.id,
            keyword=answer.keyword,
            answer=answer.answer,
            match_regex=answer.match_regex,
            match_any_keywords=answer.match_any_keywords,
            categories=[
                StandardAnswerCategorySnapshot.from_model(c) for c in answer.categories
            ],
        )


class StandardAnswerRequest(BaseModel):
    keyword: str
    answer: str
    categories: list[int] = Field(default_factory=list)
    match_regex: bool = False
    match_any_keywords: bool = False


class StandardAnswerCategoryRequest(BaseModel):
    name: str


@router.get("")
def list_standard_answers(
    _: User = Depends(_admin), db_session: Session = Depends(get_session)
) -> list[StandardAnswerSnapshot]:
    return [
        StandardAnswerSnapshot.from_model(a) for a in fetch_standard_answers(db_session)
    ]


@router.post("")
def create_standard_answer(
    request: StandardAnswerRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> StandardAnswerSnapshot:
    return StandardAnswerSnapshot.from_model(
        insert_standard_answer(
            db_session,
            keyword=request.keyword,
            answer=request.answer,
            category_ids=request.categories,
            match_regex=request.match_regex,
            match_any_keywords=request.match_any_keywords,
        )
    )


@router.patch("/{standard_answer_id}")
def patch_standard_answer(
    standard_answer_id: int,
    request: StandardAnswerRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> StandardAnswerSnapshot:
    return StandardAnswerSnapshot.from_model(
        update_standard_answer(
            db_session,
            standard_answer_id,
            keyword=request.keyword,
            answer=request.answer,
            category_ids=request.categories,
            match_regex=request.match_regex,
            match_any_keywords=request.match_any_keywords,
        )
    )


@router.delete("/{standard_answer_id}")
def delete_standard_answer(
    standard_answer_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> None:
    deactivate_standard_answer(db_session, standard_answer_id)


@router.get("/category")
def list_standard_answer_categories(
    _: User = Depends(require_permission(Permission.MANAGE_BOTS)),
    db_session: Session = Depends(get_session),
) -> list[StandardAnswerCategorySnapshot]:
    """Bot admins read these to pick a channel's categories."""
    return [
        StandardAnswerCategorySnapshot.from_model(c)
        for c in fetch_standard_answer_categories(db_session)
    ]


@router.post("/category")
def create_standard_answer_category(
    request: StandardAnswerCategoryRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> StandardAnswerCategorySnapshot:
    return StandardAnswerCategorySnapshot.from_model(
        insert_standard_answer_category(db_session, request.name)
    )


@router.patch("/category/{category_id}")
def patch_standard_answer_category(
    category_id: int,
    request: StandardAnswerCategoryRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> StandardAnswerCategorySnapshot:
    return StandardAnswerCategorySnapshot.from_model(
        update_standard_answer_category(db_session, category_id, request.name)
    )


@router.delete("/category/{category_id}")
def delete_category(
    category_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> None:
    delete_standard_answer_category(db_session, category_id)
