from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    login: str = Field(min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=1, max_length=200)
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")

    @model_validator(mode="after")
    def credential(self):
        if (self.password is None) == (self.pin is None):
            raise ValueError("Укажите пароль или ПИН")
        return self


class OperationInput(StrictModel):
    operation_id: UUID


class VersionInput(OperationInput):
    expected_version: int = Field(ge=1)


class OrderCreate(OperationInput):
    work_type: Literal["planned", "unplanned"]
    description: str = Field(min_length=3, max_length=6000)
    equipment_id: UUID
    section_id: UUID | None = None
    executor_id: UUID | None = None
    brigade_id: UUID | None = None
    shift_id: UUID | None = None
    priority: Literal["emergency", "high", "normal", "planned"] = "normal"
    due_at: datetime | None = None
    norm_minutes: int = Field(default=60, ge=1, le=43200)
    time_norm_id: UUID | None = None
    comment: str | None = Field(default=None, max_length=4000)
    equipment_stopped: bool = False

    @field_validator("due_at")
    @classmethod
    def timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Дата должна содержать часовой пояс")
        return value

    @model_validator(mode="after")
    def assignment(self):
        if (self.executor_id is None) == (self.brigade_id is None):
            raise ValueError("Выберите исполнителя или бригаду")
        if self.priority == "emergency" and self.work_type != "unplanned":
            raise ValueError("Аварийный наряд должен быть внеплановым")
        return self


class OrderAction(VersionInput):
    action: Literal["accept", "queue", "reject", "start", "pause", "resume"]
    reason: str | None = Field(default=None, min_length=2, max_length=2000)

    @model_validator(mode="after")
    def mandatory_reason(self):
        if self.action in {"reject", "pause"} and not self.reason:
            raise ValueError("Для отказа и паузы обязательна причина")
        return self


class Usage(StrictModel):
    material_id: UUID
    quantity: Decimal = Field(gt=0, le=1000000, max_digits=12, decimal_places=3)


class SubmitResult(VersionInput):
    work_description: str = Field(default="", max_length=6000)
    fault_code_id: UUID | None = None
    materials: list[Usage] = Field(default_factory=list, max_length=40)
    materials_none: bool = False
    photo_ids: list[UUID] = Field(default_factory=list, max_length=5)
    comment: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def unique_materials(self):
        if len({m.material_id for m in self.materials}) != len(self.materials):
            raise ValueError("Материал указан дважды")
        if len(set(self.photo_ids)) != len(self.photo_ids):
            raise ValueError("Фото указано дважды")
        if self.materials_none and self.materials:
            raise ValueError("Раздел материалов противоречив")
        return self


class MasterDecision(VersionInput):
    verdict: Literal["accepted", "accepted_with_remarks", "requires_rework"]
    score: int | None = Field(default=None, ge=1, le=5)
    reason: str | None = Field(default=None, min_length=2, max_length=3000)


class OrderUpdate(VersionInput):
    priority: Literal["emergency", "high", "normal", "planned"] | None = None
    due_at: datetime | None = None
    executor_id: UUID | None = None
    brigade_id: UUID | None = None
    cancel: bool = False
    reason: str = Field(min_length=2, max_length=2000)

    @field_validator("due_at")
    @classmethod
    def timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Дата должна содержать часовой пояс")
        return value


class DowntimeInput(VersionInput):
    action: Literal["start", "end"]
    reason: str = Field(min_length=2, max_length=2000)
