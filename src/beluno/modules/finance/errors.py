"""Stable, non-retryable finance errors. Details never echo amounts or descriptions."""

from __future__ import annotations

from beluno.contracts.errors import BelunoError, conflict


def amount_out_of_range(detail: str) -> BelunoError:
    return BelunoError(
        status=422, code="AMOUNT_OUT_OF_RANGE", title="Amount is out of range", detail=detail
    )


def currency_not_supported(code: str) -> BelunoError:
    return BelunoError(
        status=422,
        code="CURRENCY_NOT_SUPPORTED",
        title="Currency is not supported",
        detail=f"{code} is not a supported currency",
    )


def split_invalid(detail: str) -> BelunoError:
    return BelunoError(
        status=422, code="SPLIT_INVALID", title="Split or payer amounts are invalid", detail=detail
    )


def fx_rate_invalid(detail: str) -> BelunoError:
    return BelunoError(
        status=422, code="FX_RATE_INVALID", title="Exchange rate is invalid", detail=detail
    )


def participant_not_eligible() -> BelunoError:
    """One answer for unknown, foreign, merged, and inactive participants."""

    return BelunoError(
        status=422,
        code="PARTICIPANT_NOT_ELIGIBLE",
        title="Participant cannot take part in this entry",
        detail="Use active participants of this plan",
    )


def entry_unbalanced() -> BelunoError:
    return BelunoError(
        status=422,
        code="LEDGER_ENTRY_UNBALANCED",
        title="Ledger entry does not balance",
        detail="Adjustment postings must sum to zero",
    )


def refund_exceeds_amount() -> BelunoError:
    return conflict(
        "REFUND_EXCEEDS_AMOUNT",
        "Refunds would exceed the expense amount",
        "Refunds can never exceed the current expense amount",
    )


def fund_insufficient() -> BelunoError:
    return conflict(
        "FUND_INSUFFICIENT",
        "The plan fund does not cover this amount",
        "Record a contribution first or reduce the amount",
    )


def base_currency_locked() -> BelunoError:
    return conflict(
        "BASE_CURRENCY_LOCKED",
        "Base currency can no longer change",
        "The plan already has financial records in its base currency",
    )
