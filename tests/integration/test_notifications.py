import os

import pytest

from utils.general import send_crash_email

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def require_resend_config():
    missing = [k for k in ("RESEND_API_KEY", "CRASH_NOTIFY_EMAIL", "CRASH_NOTIFY_FROM_EMAIL") if not os.getenv(k)]
    if missing:
        pytest.skip(f"Missing in .env.testnet: {missing}")


def test_send_crash_email_delivers():
    """Calls the real Resend API and asserts a valid email ID is returned."""
    try:
        raise RuntimeError("Integration test crash — please ignore.")
    except RuntimeError as exc:
        email_id = send_crash_email(exc)

    assert email_id is not None, "send_crash_email returned None — check logs for the Resend error"
    assert isinstance(email_id, str) and len(email_id) > 0, f"Unexpected email ID: {email_id!r}"
