import os
from collections.abc import Iterator
from urllib.parse import quote

import requests
from pydantic import ValidationError

from .models import (
    AccountResponse,
    TagListResponse,
    TransactionListResponse,
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
