"""Read subscription quotas with a selected account's short-lived access token.

The installed official CLI uses this endpoint. Never refresh credentials, follow
redirects, persist the token, or send an inference request here.
"""
from datetime import datetime
import json
import math
import urllib.error
import urllib.request

from .claude_auth import ClaudeError
from .claude_borrowed_auth import read_access_token

URL = 'https://api.anthropic.com/api/oauth/usage'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize(payload):
    windows = []
    if not isinstance(payload, dict):
        return windows
    for key in ('five_hour', 'seven_day'):
        row = payload.get(key)
        if not isinstance(row, dict):
            continue
        used = row.get('utilization')
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        reset = None
        try:
            stamp = datetime.fromisoformat(row['resets_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                reset = int(stamp.timestamp())
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            pass
        windows.append(dict(key=key, used_percent=used, resets_at=reset))
    return windows


def query(root, profile, *, opener=None):
    try:
        token = read_access_token(root, profile['id'], profile['claude_account_identity'])
        request = urllib.request.Request(URL, headers={
            'Authorization': 'Bearer ' + token['accessToken'],
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'codex-workspace-usage/1',
            'Accept': 'application/json'})
        opener = opener or urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            return None, 'refresh_failed'
        windows = normalize(json.loads(raw))
        return (windows, None) if windows else (None, 'window_unavailable')
    except urllib.error.HTTPError as error:
        return None, 'usage_access_unavailable' if error.code in (401, 403) else 'refresh_failed'
    except (TimeoutError, urllib.error.URLError):
        return None, 'refresh_timeout'
    except ClaudeError:
        return None, 'usage_access_unavailable'
    except (ValueError, OSError, KeyError, TypeError):
        return None, 'refresh_failed'
