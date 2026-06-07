import re
from collections.abc import Callable
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class AccountAttrs(BaseModel):
    name: str


class AccountData(BaseModel):
    attributes: AccountAttrs


class AccountResponse(BaseModel):
    data: AccountData


class ImporterTemplate(BaseModel):
    default_account: int = Field(ge=1)
    custom_tag: str
    roles: list[str]


class CardAccount(BaseModel):
    account_id: int = Field(ge=1)
    abbreviation: str = ""


class TemplateInfo(BaseModel):
    path: Path
    filename_pattern: str | None = None
    csv_column_header: str | None = None
    preprocessor: Callable[[bytes], tuple[bytes, str]] | None = None

    @field_validator("filename_pattern")
    @classmethod
    def _must_have_capture_group(cls, value: str | None) -> str | None:
        if value is not None and re.compile(value).groups < 1:
            raise ValueError("filename_pattern must contain at least one capture group")
        return value

    @model_validator(mode="after")
    def _at_most_one_lookup_source(self) -> Self:
        if self.filename_pattern is not None and self.csv_column_header is not None:
            raise ValueError("at most one of filename_pattern or csv_column_header may be set")
        return self


AccountMappingsAdapter: TypeAdapter[dict[str, dict[str, CardAccount]]] = TypeAdapter(
    dict[str, dict[str, CardAccount]]
)
TemplateDictAdapter: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


class Args(BaseModel):
    path: str
    template: str | None
    dry_run: bool
    no_color: bool
