"""Supabase-verified, allowlisted staff sessions. Never store passwords."""
import json, os, threading, time
from http.cookies import SimpleCookie
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

COOKIE = '__Host-cs-session'
_attempts = []
_lock = threading.Lock()

def approved(email):
    email = str(email).strip().lower()
    domain = os.environ.get('CS_STAFF_DOMAIN','').strip().lower()
    return bool(email) and (email in {e.strip().lower() for e in os.environ.get('CS_STAFF_EMAILS','').split(',') if e.strip()} or
                            (bool(domain) and email.count('@')==1 and email.split('@')[1]==domain))

def configured():
    return (os.environ.get('SUPABASE_URL','').startswith('https://') and
            bool(os.environ.get('SUPABASE_PUBLISHABLE_KEY')) and bool(os.environ.get('CS_STAFF_EMAILS','').strip() or os.environ.get('CS_STAFF_DOMAIN','').strip()))

def request(path, token='', body=None):
    headers = {'apikey':os.environ['SUPABASE_PUBLISHABLE_KEY'], 'Content-Type':'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    req = Request(os.environ['SUPABASE_URL'].rstrip('/')+'/auth/v1/'+path,
                  data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urlopen(req, timeout=15) as response:
        return json.load(response)

def identity(user):
    email = str(user.get('email','')).lower()
    return email if user.get('id') and user.get('email_confirmed_at') and approved(email) else None

def current(cookie_header):
    try:
        cookies = SimpleCookie(); cookies.load(cookie_header or '')
        token = cookies[COOKIE].value if COOKIE in cookies else ''
        return identity(request('user',token=token)) if token else None
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
        return None

def login(email, password):
    with _lock:
        now = time.monotonic()
        _attempts[:] = [t for t in _attempts if now-t < 60]
        if len(_attempts) >= 10: raise ValueError('Too many attempts. Wait one minute and try again.')
        _attempts.append(now)
    if not approved(email): raise ValueError('Sign-in failed. Check your credentials or contact your administrator.')
    try:
        result = request('token?grant_type=password', body={'email':email,'password':password})
        if not identity(result.get('user',{})): raise ValueError('Account is not approved or confirmed.')
        token = result['access_token']
        if not token or any(c in token for c in '\r\n;'): raise ValueError('Invalid session.')
        return f'{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={min(int(result.get("expires_in",3600)),3600)}'
    except (HTTPError, URLError, TimeoutError, KeyError):
        raise ValueError('Sign-in failed. Check your credentials or try again shortly.') from None

def logout_cookie():
    return f'{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'
