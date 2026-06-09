import os
from collections.abc import Iterator
from urllib.parse import quote

import requests
from pydantic import ValidationError

from .models import (
    AccountResponse,
    AutocompleteCategoryListAdapter,
    BudgetLimitData,
    BudgetLimitListResponse,
    CategoryListResponse,
    InsightGroupAdapter,
    InsightGroupEntry,
    InsightTotalAdapter,
    InsightTotalEntry,
    TagListResponse,
    TransactionListResponse,
    TransactionSingleResponse,
    TransactionSplit,
)

_PAGE_LIMIT = 50
_TIMEOUT = 30


def _auth_context() -> tuple[str, dict[str, str]] | None:
    """Return ``(base_url, headers)`` for Firefly III API calls.

    Returns ``None`` when ``FIREFLY_III_URL`` or ``FIREFLY_III_PAT`` is
    unset, mirroring the best-effort behaviour expected by
    :func:`lookup_account_name`. Callers that need hard failures (the
    export script) should resolve the env vars themselves and pass the
    values via :func:`_auth_context_strict`.
    """
    try:
        url = os.environ["FIREFLY_III_URL"].rstrip("/")
        token = os.environ["FIREFLY_III_PAT"]
    except KeyError:
        return None
    return url, _headers(token)


def _auth_context_strict() -> tuple[str, dict[str, str]]:
    """Like :func:`_auth_context` but raises ``KeyError`` if env vars are missing."""
    url = os.environ["FIREFLY_III_URL"].rstrip("/")
    token = os.environ["FIREFLY_III_PAT"]
    return url, _headers(token)


def _headers(token: str) -> dict[str, str]:
    return {
        "accept": "application/vnd.api+json",
        "Authorization": f"Bearer {token}",
    }


def lookup_account_name(account_id: int) -> str | None:
    """Best-effort lookup of a Firefly III asset account's display name."""
    ctx = _auth_context()
    if ctx is None:
        return None
    url, headers = ctx
    try:
        response = requests.get(
            f"{url}/api/v1/accounts/{account_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return AccountResponse.model_validate(response.json()).data.attributes.name
    except (requests.RequestException, ValidationError):
        return None


def iter_tags() -> Iterator[str]:
    """Yield every tag name in the Firefly III instance, paginated.

    Reads ``FIREFLY_III_URL`` / ``FIREFLY_III_PAT`` from the environment
    (raises ``KeyError`` if either is unset). ``requests.RequestException``
    and ``pydantic.ValidationError`` propagate so the caller can fail
    loudly instead of silently dropping pages.
    """
    url, headers = _auth_context_strict()
    page = 1
    while True:
        response = requests.get(
            f"{url}/api/v1/tags",
            headers=headers,
            params={"limit": _PAGE_LIMIT, "page": page},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = TagListResponse.model_validate(response.json())
        for entry in body.data:
            yield entry.attributes.tag
        if page >= body.meta.pagination.total_pages:
            return
        page += 1


def iter_transactions_for_tag(tag: str) -> Iterator[tuple[str, list[TransactionSplit]]]:
    """Yield ``(transaction_id, splits)`` for every transaction in ``tag``.

    The ``transaction_id`` is the Firefly III transaction group id (the
    ``data[].id`` field on the API response). ``splits`` is the
    transaction's ``attributes.transactions`` list as-is, so the caller
    can enforce single-split invariants without peeking ahead. Pages
    automatically; errors propagate (see :func:`iter_tags`).

    ``tag`` is URL-encoded (``urllib.parse.quote`` with no safe chars)
    before being interpolated into the path so tags containing spaces,
    slashes, or other reserved characters round-trip correctly. Firefly
    III's path parameter accepts either the literal tag string or the
    numeric id.
    """
    url, headers = _auth_context_strict()
    encoded_tag = quote(tag, safe="")
    page = 1
    while True:
        response = requests.get(
            f"{url}/api/v1/tags/{encoded_tag}/transactions",
            headers=headers,
            params={"limit": _PAGE_LIMIT, "page": page},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = TransactionListResponse.model_validate(response.json())
        for entry in body.data:
            yield entry.id, entry.attributes.transactions
        if page >= body.meta.pagination.total_pages:
            return
        page += 1


def get_transaction_journal(journal_id: str) -> tuple[str, list[TransactionSplit]] | None:
    """Return ``(group_id, splits)`` for a single transaction journal, or ``None`` on 404.

    Wraps ``GET /api/v1/transaction-journals/{id}``, which returns the
    parent transaction group containing the requested journal. The
    group id is the value the ``PUT /api/v1/transactions/{id}``
    endpoint expects in its path. Other ``requests.RequestException``
    subclasses and ``pydantic.ValidationError`` propagate so the
    caller can fail loudly.
    """
    url, headers = _auth_context_strict()
    response = requests.get(
        f"{url}/api/v1/transaction-journals/{journal_id}",
        headers=headers,
        timeout=_TIMEOUT,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = TransactionSingleResponse.model_validate(response.json())
    return body.data.id, body.data.attributes.transactions


def update_transaction_category(
    group_id: str,
    journal_id: str,
    category_name: str,
) -> None:
    """Set ``category_name`` on a single split via ``PUT /api/v1/transactions/{group_id}``.

    Including ``transaction_journal_id`` in the request body tells
    Firefly III to update *that* split in place rather than replacing
    the entire transactions array on the group — important when a
    group has more than one split, and harmless for single-split
    transactions. Raises ``requests.HTTPError`` on non-2xx so callers
    can surface the failure.
    """
    url, headers = _auth_context_strict()
    payload = {
        "transactions": [
            {
                "transaction_journal_id": journal_id,
                "category_name": category_name,
            }
        ]
    }
    response = requests.put(
        f"{url}/api/v1/transactions/{group_id}",
        headers={**headers, "Content-Type": "application/json"},
        json=payload,
        timeout=_TIMEOUT,
    )
    response.raise_for_status()


def iter_categories() -> Iterator[str]:
    """Yield every category name in the Firefly III instance, paginated.

    Reads ``FIREFLY_III_URL`` / ``FIREFLY_III_PAT`` from the environment
    (raises ``KeyError`` if either is unset). ``requests.RequestException``
    and ``pydantic.ValidationError`` propagate so the caller can fail
    loudly instead of silently dropping pages.
    """
    url, headers = _auth_context_strict()
    page = 1
    while True:
        response = requests.get(
            f"{url}/api/v1/categories",
            headers=headers,
            params={"limit": _PAGE_LIMIT, "page": page},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = CategoryListResponse.model_validate(response.json())
        for entry in body.data:
            yield entry.attributes.name
        if page >= body.meta.pagination.total_pages:
            return
        page += 1


def lookup_category_id(name: str) -> str | None:
    """Return the Firefly III id of the category named ``name``, or ``None``.

    Uses the ``GET /api/v1/autocomplete/categories`` endpoint, which is
    purpose-built for name-to-id resolution: it accepts a ``query``
    parameter and returns a flat ``[{id, name}]`` array (much cheaper
    than paginating ``/api/v1/categories``). The autocomplete match is
    substring-ish, so we filter the response for an exact name match
    before returning the id.

    The autocomplete endpoint serves ``application/json`` (not the
    JSON-API media type the rest of the API uses), so we override the
    ``Accept`` header here.
    """
    url, headers = _auth_context_strict()
    response = requests.get(
        f"{url}/api/v1/autocomplete/categories",
        headers={**headers, "accept": "application/json"},
        params={"query": name},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    entries = AutocompleteCategoryListAdapter.validate_python(response.json())
    for entry in entries:
        if entry.name == name:
            return entry.id
    return None


def iter_transactions_for_category(
    category_id: str,
    *,
    start: str,
    end: str,
) -> Iterator[tuple[str, list[TransactionSplit]]]:
    """Yield ``(transaction_id, splits)`` for every transaction in a category.

    Wraps ``GET /api/v1/categories/{id}/transactions`` with ``start``
    and ``end`` query parameters (both ``YYYY-MM-DD``, inclusive).
    Mirrors :func:`iter_transactions_for_tag` exactly: same pagination
    shape, same response model, same yield contract.
    """
    url, headers = _auth_context_strict()
    page = 1
    while True:
        response = requests.get(
            f"{url}/api/v1/categories/{category_id}/transactions",
            headers=headers,
            params={
                "limit": _PAGE_LIMIT,
                "page": page,
                "start": start,
                "end": end,
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = TransactionListResponse.model_validate(response.json())
        for entry in body.data:
            yield entry.id, entry.attributes.transactions
        if page >= body.meta.pagination.total_pages:
            return
        page += 1


def iter_budget_limits(*, start: str, end: str) -> Iterator[BudgetLimitData]:
    """Yield every budget limit returned by ``GET /api/v1/budget-limits``.

    Both ``start`` and ``end`` (``YYYY-MM-DD``, inclusive) are required
    by the Firefly III endpoint. The endpoint returns every budget
    limit whose own date range overlaps the query range, with each
    limit's own ``amount`` (the budgeted figure) and ``spent`` array
    (per-currency totals attributed to that limit). Pagination, auth,
    and error propagation mirror :func:`iter_transactions_for_category`.
    """
    url, headers = _auth_context_strict()
    page = 1
    while True:
        response = requests.get(
            f"{url}/api/v1/budget-limits",
            headers=headers,
            params={
                "limit": _PAGE_LIMIT,
                "page": page,
                "start": start,
                "end": end,
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = BudgetLimitListResponse.model_validate(response.json())
        yield from body.data
        if page >= body.meta.pagination.total_pages:
            return
        page += 1


def iter_insight_expense_categories(
    *,
    start: str,
    end: str,
) -> Iterator[InsightGroupEntry]:
    """Yield every entry from ``GET /api/v1/insight/expense/category``.

    Both ``start`` and ``end`` (``YYYY-MM-DD``, inclusive) are required
    by Firefly III. The endpoint returns a flat ``application/json``
    array (not JSON-API and not paginated) with one entry per
    ``(category, currency)`` pair whose expenses fall in the range.
    The ``Accept`` header is overridden to ``application/json`` to
    match the endpoint's content type (same trick as
    :func:`lookup_category_id`).
    """
    url, headers = _auth_context_strict()
    response = requests.get(
        f"{url}/api/v1/insight/expense/category",
        headers={**headers, "accept": "application/json"},
        params={"start": start, "end": end},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    yield from InsightGroupAdapter.validate_python(response.json())


def iter_insight_expense_no_category(
    *,
    start: str,
    end: str,
) -> Iterator[InsightTotalEntry]:
    """Yield every entry from ``GET /api/v1/insight/expense/no-category``.

    Sister of :func:`iter_insight_expense_categories` for the
    uncategorized-only bucket: same response shape minus the ``id`` /
    ``name`` fields, since there is a single bucket split per-currency.
    """
    url, headers = _auth_context_strict()
    response = requests.get(
        f"{url}/api/v1/insight/expense/no-category",
        headers={**headers, "accept": "application/json"},
        params={"start": start, "end": end},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    yield from InsightTotalAdapter.validate_python(response.json())
