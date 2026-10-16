"""authenticate() must stop when the session is in a different org than configured."""
import pytest

from scripts.lib.ts_client import AuthError, TSClient, session_org_id


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.text = status, data, ""

    def json(self):
        return self._data


class _Session:
    def __init__(self, me):
        self.headers, self.me = {}, me

    def post(self, *a, **k):
        return _Resp(200, {"token": "tok"})

    def get(self, *a, **k):
        if isinstance(self.me, Exception):
            raise self.me
        return self.me


def _client(me, org_id=426706049):
    c = TSClient("https://ts", org_id, "u", "k")
    c._session = _Session(me)
    return c


def test_session_org_id_reads_current_org():
    assert session_org_id({"current_org": {"id": 7, "name": "x"}}) == 7
    assert session_org_id({"current_org": {"id": 0, "name": "Primary"}}) == 0
    assert session_org_id({"org_id": 5}) == 5
    assert session_org_id({"orgs": [{"id": 1, "name": "a"}]}) is None      # not the current org
    assert session_org_id({}) is None


def test_matching_org_authenticates_and_logs_the_real_org(capsys):
    c = _client(_Resp(200, {"name": "svc", "current_org": {"id": 426706049, "name": "Prod"}}))
    c.authenticate()
    assert c._token == "tok"
    assert "session_org=426706049 configured_org=426706049" in capsys.readouterr().err


def test_org_id_given_as_a_string_still_matches():
    _client(_Resp(200, {"current_org": {"id": 426706049}}), org_id="426706049").authenticate()


def test_a_different_org_aborts_and_drops_the_token():
    c = _client(_Resp(200, {"name": "svc", "current_org": {"id": 0, "name": "Primary"}}))
    with pytest.raises(AuthError) as e:
        c.authenticate()
    assert "org 0" in str(e.value) and "426706049" in str(e.value)
    assert c._token is None


@pytest.mark.parametrize("me", [_Resp(200, {"name": "svc"}), _Resp(500, {}), _Resp(403, {}),
                                RuntimeError("down")])
def test_an_unverifiable_session_warns_but_continues(me, capsys):
    c = _client(me)
    c.authenticate()                                   # cannot verify: no abort
    assert c._token == "tok"
    assert "session_org=unknown" in capsys.readouterr().err
