"""App Store verification failures that are not the purchase's fault."""

from __future__ import annotations

import pytest
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus

from beluno.config import Settings
from beluno.stores import AppleStore, PurchaseInvalid, StoreUnavailable, _verification_error


def test_unreadable_root_certificates_leave_the_store_unavailable() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        apple_bundle_id="app.beluno.test",
        apple_root_certificates=["/nonexistent/AppleRootCA-G3.cer"],
    )
    with pytest.raises(StoreUnavailable):
        AppleStore.from_settings(settings)


def test_an_unanswered_certificate_check_is_retried_not_refused() -> None:
    retry = VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE)
    assert isinstance(_verification_error(retry), StoreUnavailable)
    forged = VerificationException(VerificationStatus.VERIFICATION_FAILURE)
    assert isinstance(_verification_error(forged), PurchaseInvalid)
