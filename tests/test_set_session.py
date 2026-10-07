"""The session comes out of a browser's curl into .env, values unprinted."""
import os
import stat

from notifier.envfile import read_env, write_env
from tools.set_session import parse_curl

CURL = r"""curl 'https://api.buff.market/api/market/goods/sell_order?game=csgo&goods_id=5777' \
  -H 'Accept: application/json, text/plain, */*' \
  -b 'session=1-abc; client_id=xyz; csrf_token=t0k' \
  -H 'X-CSRFToken: Ij.abc.def' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64)' \
  --compressed"""


def test_the_cookie_token_and_agent_are_found():
    assert parse_curl(CURL) == {
        "BUFF_COOKIE": "session=1-abc; client_id=xyz; csrf_token=t0k",
        "BUFF_CSRF": "Ij.abc.def",
        "BUFF_USER_AGENT": "Mozilla/5.0 (X11; Linux x86_64)",
    }


def test_a_cookie_header_counts_as_much_as_dash_b():
    curl = "curl 'https://x' -H 'Cookie: session=1' -H 'user-agent: UA'"
    assert parse_curl(curl) == {"BUFF_COOKIE": "session=1", "BUFF_USER_AGENT": "UA"}


def test_env_keeps_other_settings_and_is_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CSFLOAT_DB_PATH=/x\nBUFF_COOKIE=old\n")
    write_env(env, {"BUFF_COOKIE": "session=1; a=b"})
    assert env.read_text() == "CSFLOAT_DB_PATH=/x\nBUFF_COOKIE='session=1; a=b'\n"
    assert read_env(env)["BUFF_COOKIE"] == "session=1; a=b"
    write_env(env, {"BUFF_COOKIE": None})
    assert "BUFF_COOKIE" not in read_env(env)
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_windows_cmd_curl_is_understood_too():
    cmd = ('curl ^"https://api.buff.market/api/market/goods/sell_order?game=csgo^&goods_id=5777^" ^\n'
           '  -H ^"accept: application/json^" ^\n'
           '  -b ^"session=1-abc; client_id=xyz^" ^\n'
           '  -H ^"x-csrftoken: Ij.abc^" ^\n'
           '  -H ^"user-agent: Mozilla/5.0 (Windows NT 10.0)^"')
    assert parse_curl(cmd) == {"BUFF_COOKIE": "session=1-abc; client_id=xyz",
                               "BUFF_CSRF": "Ij.abc",
                               "BUFF_USER_AGENT": "Mozilla/5.0 (Windows NT 10.0)"}
