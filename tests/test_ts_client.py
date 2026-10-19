"""Tests for TSClient: auth, retry, token refresh, secret masking."""
import time
import pytest
from unittest.mock import MagicMock, patch, call

from scripts.lib.ts_client import AuthError, TSClient


BASE_URL = "https://dev.example.com"
USERNAME = "admin"
SECRET_KEY = "super_secret_key"
ORG_ID = 12


@pytest.fixture
def client():
    c = TSClient(BASE_URL, ORG_ID, USERNAME, SECRET_KEY)
    # authenticate() also asks which org the session landed in; answer it without a network call.
    c._session.get = MagicMock(return_value=MagicMock(
        status_code=200,
        json=MagicMock(return_value={"name": USERNAME, "current_org": {"id": ORG_ID}})))
    return c


class TestAuthenticate:
    def test_stores_token(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "token": "tok_abc",
            "token_validity_duration_in_sec": 3600,
            "org_id": ORG_ID,
        }
        with patch("requests.Session.post", return_value=mock_resp):
            client.authenticate()
        assert client._token == "tok_abc"

    def test_secret_not_in_exception_message(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch("requests.Session.post", return_value=mock_resp):
            with pytest.raises(AuthError) as exc:
                client.authenticate()
        assert SECRET_KEY not in str(exc.value)
        assert "REDACTED" in str(exc.value)


class TestRetry:
    def test_retries_on_429(self, client):
        client._token = "tok"
        client._token_expires_at = time.time() + 3600

        fail_resp = MagicMock(status_code=429, headers={"Retry-After": "0"})
        fail_resp.raise_for_status.side_effect = Exception("429")
        ok_resp = MagicMock(status_code=200)
        ok_resp.raise_for_status.return_value = None
        ok_resp.json.return_value = {"data": []}

        with patch("scripts.lib.ts_client.time.sleep"), \
             patch.object(client._session, "request",
                          side_effect=[fail_resp, ok_resp]) as mock_req:
            result = client.request("GET", "/metadata/search")
        assert mock_req.call_count == 2

    def test_no_retry_on_400(self, client):
        """A client error is handed back to the caller after one attempt, not retried."""
        client._token = "tok"
        client._token_expires_at = time.time() + 3600

        bad_resp = MagicMock(status_code=400)

        with patch.object(client._session, "request", return_value=bad_resp) as mock_req:
            result = client.request("GET", "/metadata/search")
        assert result is bad_resp
        assert mock_req.call_count == 1


class TestTokenRefresh:
    """The 60s early-refresh buffer is applied when the expiry is stored, so the check is plain."""

    def _token_response(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"token": "new_tok"}
        return mock_resp

    def test_reauthenticates_when_the_token_has_expired(self, client):
        client._token = "old_tok"
        client._token_expires_at = time.time() - 1

        with patch("requests.Session.post", return_value=self._token_response()):
            client._ensure_token()
        assert client._token == "new_tok"

    def test_keeps_a_token_that_is_still_valid(self, client):
        client._token = "old_tok"
        client._token_expires_at = time.time() + 3600

        with patch("requests.Session.post") as post:
            client._ensure_token()
        assert client._token == "old_tok"
        post.assert_not_called()

    def test_stored_expiry_leaves_the_buffer_before_the_real_expiry(self, client):
        with patch("requests.Session.post", return_value=self._token_response()):
            client.authenticate()
        assert client._token_expires_at < time.time() + client._token_ttl_sec
