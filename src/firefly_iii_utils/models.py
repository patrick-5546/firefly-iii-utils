import re
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
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


class Pagination(BaseModel):
    current_page: int = Field(ge=1)
    total_pages: int = Field(ge=0)


class Meta(BaseModel):
    pagination: Pagination


class TagAttrs(BaseModel):
    tag: str


class TagData(BaseModel):
    attributes: TagAttrs


class TagListResponse(BaseModel):
    data: list[TagData]
    meta: Meta


class TransactionSplit(BaseModel):
    """Subset of the Firefly III TransactionSplit schema used by the export script.

    The real schema has dozens of fields; ``extra="ignore"`` drops the
    rest silently so a schema bump doesn't break the model.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    transaction_journal_id: str
    description: str
    amount: str
    date: str
    type: str
    source_name: str | None = None
    destination_name: str | None = None
    category_id: str | None = None
    category_name: str | None = None


class TransactionAttrs(BaseModel):
    transactions: list[TransactionSplit]


class TransactionData(BaseModel):
    id: str
    attributes: TransactionAttrs


class TransactionListResponse(BaseModel):
    data: list[TransactionData]
    meta: Meta


class TransactionSingleResponse(BaseModel):
    """Response shape for ``GET /api/v1/transaction-journals/{id}``.

    Returns the full transaction group containing the requested
    journal, just without a ``meta`` block (it's a single-resource
    response, not paginated).
    """

    data: TransactionData


class CategoryAttrs(BaseModel):
    name: str


class CategoryData(BaseModel):
    attributes: CategoryAttrs


class CategoryListResponse(BaseModel):
    data: list[CategoryData]
    meta: Meta


class AutocompleteCategoryEntry(BaseModel):
    """One entry from ``GET /api/v1/autocomplete/categories``.

    The autocomplete endpoint returns a flat ``application/json``
    array (not a JSON-API document), so this model is consumed via a
    :class:`pydantic.TypeAdapter` rather than wrapped in a
    list-response model with ``data`` / ``meta`` fields.
    """

    id: str
    name: str


AutocompleteCategoryListAdapter: TypeAdapter[list[AutocompleteCategoryEntry]] = TypeAdapter(
    list[AutocompleteCategoryEntry]
)


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


class ExportArgs(BaseModel):
    prefix: str
    model: str
    no_guess: bool
    no_color: bool


class ImportCategoriesArgs(BaseModel):
    path: str
    dry_run: bool
    no_color: bool


class FindUnmatchedTransfersArgs(BaseModel):
    start: str
    end: str | None
    reviewed: str | None
    no_color: bool


class CompareGuessesArgs(BaseModel):
    path: str
    top_confusions: int = Field(ge=1)
    no_color: bool


class CurrencySumEntry(BaseModel):
    """One entry in a Firefly III per-currency sum array.

    Mirrors the ``ArrayEntryWithCurrencyAndSum`` schema used by budget
    limits (and other models) to report amounts split across currencies.
    ``extra="ignore"`` drops the optional symbol / decimal-places /
    currency_id fields that the script does not need so a schema bump
    cannot break the model.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    currency_code: str
    sum: str


class BudgetLimitAttrs(BaseModel):
    """Subset of the Firefly III BudgetLimit schema used by sum-budget-diffs.

    ``start`` / ``end`` are ISO 8601 date-times; the script parses them
    via :func:`firefly_iii_utils.parsing.parse_date` and enforces that
    each limit's range falls within a single calendar month.
    ``extra="ignore"`` matches the convention used by
    :class:`TransactionSplit`.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    start: str
    end: str
    amount: str
    currency_code: str
    spent: list[CurrencySumEntry] = []


class BudgetLimitData(BaseModel):
    id: str
    attributes: BudgetLimitAttrs


class BudgetLimitListResponse(BaseModel):
    data: list[BudgetLimitData]
    meta: Meta


class SumBudgetDiffsArgs(BaseModel):
    year: int = Field(ge=1900, le=9999)
    currency: str
    no_color: bool
