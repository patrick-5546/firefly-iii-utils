"""GitHub Copilot-backed category guessing for uncategorized transactions.

Kept separate from :mod:`firefly_iii_utils.guess_categories` so the
heaviest dependencies on this codebase (the ``github-copilot-sdk``
package and an async event loop) live in a single swappable module.
The CLI wrapper in ``guess_categories.py`` orchestrates the Firefly
III calls and CSV emission, then defers to :func:`guess` here for the
actual LLM round-trip.
"""

from collections.abc import Sequence
from typing import Protocol

from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.session_events import AssistantMessageData
from pydantic import TypeAdapter, ValidationError
from rich.console import Console

DEFAULT_MODEL = "gpt-5-mini"
LLM_TIMEOUT_SECONDS = 180.0

_GuessesAdapter: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


class TransactionLike(Protocol):
    """Structural type for a transaction the LLM can categorize.

    Any object with these (read-only) attributes — e.g. ``Row`` in
    :mod:`firefly_iii_utils.guess_categories`, which is a
    :class:`typing.NamedTuple` — can be passed to :func:`guess`. The
    protocol keeps that module from having to import a concrete
    ``Row`` type and creating a cycle. Fields are declared as
    properties so a ``NamedTuple`` (whose fields are read-only)
    satisfies the protocol structurally.
    """

    @property
    def transaction_journal_id(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def amount(self) -> str: ...
    @property
    def date(self) -> str: ...
    @property
    def source_account(self) -> str: ...
    @property
    def destination_account(self) -> str: ...


def _strip_json_fences(text: str) -> str:
    """Strip ```json / ``` code fences (and surrounding whitespace) if present.

    Tolerant of either ``\\`\\`\\`json`` or ``\\`\\`\\``` opening fences
    and either ``\\`\\`\\``` or end-of-string closings, so we accept
    LLM responses that wrap the JSON in markdown despite being asked
    not to.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1 :] if first_newline != -1 else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _build_prompt(rows: Sequence[TransactionLike], categories: Sequence[str]) -> str:
    category_block = "\n".join(f"- {c}" for c in categories)
    transaction_lines: list[str] = []
    for row in rows:
        transaction_lines.append(
            f"- id={row.transaction_journal_id} | "
            + f"description={row.description!r} | "
            + f"amount={row.amount} | "
            + f"date={row.date} | "
            + f"source={row.source_account!r} | "
            + f"destination={row.destination_account!r}"
        )
    transaction_block = "\n".join(transaction_lines)
    return (
        "You are categorizing personal finance transactions. For each transaction below, "
        "pick the single best-fitting category from the allowed list. If none clearly "
        "fits, return an empty string for that transaction.\n\n"
        'Allowed Categories (use exactly one of these strings, case-sensitive, or ""):\n'
        f"{category_block}\n\n"
        "Transactions:\n"
        f"{transaction_block}\n\n"
        "Respond with ONLY a single raw JSON object mapping each transaction id (as a "
        "string) to its chosen category (as a string). Do not include prose, markdown, "
        "code fences, or any other text. Example shape:\n"
        '{"123": "Groceries", "456": ""}'
    )


async def guess(
    rows: Sequence[TransactionLike],
    categories: Sequence[str],
    *,
    model: str,
    console: Console,
) -> dict[str, str]:
    """Ask GitHub Copilot to pick a category for each row.

    Returns a raw ``{transaction_journal_id: category_name}`` mapping
    straight from the LLM (no whitelist filtering — that's
    :func:`validate`'s job). Skips the LLM call entirely when ``rows``
    is empty.
    """
    if not rows:
        return {}
    prompt = _build_prompt(rows, categories)
    console.print(
        f"Asking Copilot ({model}) to categorize {len(rows)} transaction(s)\u2026",
        highlight=False,
    )
    async with (
        CopilotClient() as client,
        await client.create_session(
            model=model,
            on_permission_request=PermissionHandler.approve_all,
            available_tools=[],
        ) as session,
    ):
        event = await session.send_and_wait(prompt, timeout=LLM_TIMEOUT_SECONDS)
    if event is None or not isinstance(event.data, AssistantMessageData):
        raise RuntimeError(
            "Copilot returned no assistant message before becoming idle; "
            + "cannot extract category guesses."
        )
    raw = _strip_json_fences(event.data.content)
    try:
        return _GuessesAdapter.validate_json(raw)
    except ValidationError as exc:
        snippet = raw[:500]
        raise RuntimeError(
            "Copilot response was not a JSON object mapping transaction id -> category "
            + f"string. First 500 chars of stripped response: {snippet!r}. "
            + f"Validation error: {exc}"
        ) from exc


def validate(
    raw: dict[str, str],
    allowed: set[str],
    journal_ids: set[str],
    console: Console,
) -> dict[str, str]:
    """Filter LLM picks down to keys we asked about and categories that exist.

    Each drop is logged to ``console`` (stderr) so the user can see why
    a row ended up blank in the CSV.
    """
    valid: dict[str, str] = {}
    for journal_id, category in raw.items():
        if journal_id not in journal_ids:
            console.print(
                f"ignoring Copilot pick for unknown transaction id {journal_id!r}",
                highlight=False,
            )
            continue
        category_clean = category.strip()
        if not category_clean:
            continue
        if category_clean not in allowed:
            console.print(
                f"ignoring Copilot pick for {journal_id!r}: "
                + f"category {category_clean!r} is not one of the existing categories",
                highlight=False,
            )
            continue
        valid[journal_id] = category_clean
    return valid
