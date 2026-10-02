"""Administrator-managed app accounts and revocable, hashed sessions."""
import hashlib, hmac, json, os, secrets, time
from http.cookies import SimpleCookie
import staff_auth

OWNER = 'jvgerong@findme.com.ph'

def setup(connect,now):
    with connect() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS app_users(email TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL,password_hash TEXT NOT NULL,provider TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_sessions(token_hash TEXT PRIMARY KEY,email TEXT NOT NULL,expires BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS access_audit(id TEXT PRIMARY KEY,actor TEXT NOT NULL,at TEXT NOT NULL,action TEXT NOT NULL,email TEXT NOT NULL);
        ''')
        con.execute('INSERT INTO app_users VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(email) DO NOTHING',(OWNER,'CS Administrator','admin',1,'','supabase',1,now()))

def account(server,email):
    with server.connect() as con:
        row=con.execute('SELECT * FROM app_users WHERE email=?',(email,)).fetchone()
        return dict(row) if row else None

def is_admin(server,email):
    r=account(server,email) if email else None
    return bool(r and r['active'] and r['role']=='admin')

def require_admin(server,email):
    if not is_admin(server,email): raise PermissionError('Only administrators can manage user access.')

def digest(value): return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password):
    if not isinstance(password,str) or not 12<=len(password)<=256: raise ValueError('Use a password of 12–256 characters.')
    salt=secrets.token_hex(16)
    key=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return salt+':'+key

def password_matches(password,stored):
    try:
        if len(password)>256:return False
        salt,key=stored.split(':')
        actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(actual,key)
    except (ValueError,TypeError):return False

def current(server,header):
    try:
        cookies=SimpleCookie();cookies.load(header or '')
        token=cookies[staff_auth.COOKIE].value if staff_auth.COOKIE in cookies else ''
        if token.startswith('csd_'):
            with server.connect() as con:
                row=con.execute('SELECT email FROM app_sessions WHERE token_hash=? AND expires>?',(digest(token),int(time.time()))).fetchone()
            email=row['email'] if row else None
        else: email=staff_auth.current(header)
        r=account(server,email) if email else None
        return email if r and r['active'] and staff_auth.approved(email) and (token.startswith('csd_') or r['provider']=='supabase') else None
    except (ValueError,KeyError):return None

def login(server,email,password):
    # Applies equally to unknown users and valid users, without revealing existence.
    with staff_auth._lock:
        now=time.monotonic();staff_auth._attempts[:]=[t for t in staff_auth._attempts if now-t<60]
        if len(staff_auth._attempts)>=10:raise ValueError('Try again in one minute.')
        staff_auth._attempts.append(now)
    r=account(server,email)
    if not r or not r['active'] or not staff_auth.approved(email):raise ValueError('Sign-in failed.')
    if r['provider']=='supabase':return staff_auth.login(email,password)
    if not password_matches(password,r['password_hash']):raise ValueError('Sign-in failed.')
    token='csd_'+secrets.token_urlsafe(32)
    with server.connect() as con:
        con.execute('DELETE FROM app_sessions WHERE expires<=?',(int(time.time()),))
        con.execute('INSERT INTO app_sessions VALUES(?,?,?)',(digest(token),email,int(time.time())+3600))
    return f'{staff_auth.COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600'

def logout(server,header):
    cookies=SimpleCookie();cookies.load(header or '')
    if staff_auth.COOKIE in cookies:
        with server.connect() as con:con.execute('DELETE FROM app_sessions WHERE token_hash=?',(digest(cookies[staff_auth.COOKIE].value),))

def listing(server,actor):
    require_admin(server,actor)
    with server.connect() as con:
        users=[dict(r) for r in con.execute('SELECT email,name,role,active,provider,version,created FROM app_users ORDER BY email')]
        audit=[dict(r) for r in con.execute('SELECT actor,at,action,email FROM access_audit ORDER BY at DESC LIMIT 30')]
    return {'users':users,'audit':audit,'currentUser':actor}

def save(server,payload,actor):
    require_admin(server,actor)
    email=str(payload.get('email','')).strip().lower();action=payload.get('action')
    if not staff_auth.approved(email) or email.count('@')!=1 or any(c.isspace() for c in email):raise ValueError('Use an approved work email address.')
    with server.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        # Recheck authorization under the same write lock.
        admin=con.execute('SELECT role,active FROM app_users WHERE email=?',(actor,)).fetchone()
        if not admin or not admin['active'] or admin['role']!='admin':raise PermissionError('Administrator access required.')
        row=con.execute('SELECT * FROM app_users WHERE email=?',(email,)).fetchone()
        if action=='create':
            if row:raise ValueError('This account already exists.')
            name=str(payload.get('name','')).strip()
            if not name or len(name)>120:raise ValueError('Enter a name up to 120 characters.')
            hashed=password_hash(payload.get('password'))
            con.execute('INSERT INTO app_users VALUES(?,?,?,?,?,?,?,?)',(email,name,'staff',1,hashed,'app',1,server.now()))
        else:
            if not row or row['version']!=payload.get('version'):raise ValueError('Account changed. Refresh User Access.')
            if action=='password':
                hashed=password_hash(payload.get('password'))
                con.execute("UPDATE app_users SET password_hash=?,provider='app',version=version+1 WHERE email=?",(hashed,email))
            elif action in ('enable','disable'):
                if email==actor and action=='disable':raise ValueError('You cannot disable your own account.')
                con.execute('UPDATE app_users SET active=?,version=version+1 WHERE email=?',(int(action=='enable'),email))
            else:raise ValueError('Unknown account action.')
            con.execute('DELETE FROM app_sessions WHERE email=?',(email,))
        con.execute('INSERT INTO access_audit VALUES(?,?,?,?,?)',(secrets.token_hex(16),actor,server.now(),action,email))
    return {'ok':True}

def issue_sync_key(server,actor):
    require_admin(server,actor);token=secrets.token_urlsafe(48)
    with server.connect() as con:
        con.execute("INSERT INTO settings(key,value) VALUES('incoming_sync_key',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(digest(token),))
        con.execute('INSERT INTO access_audit VALUES(?,?,?,?,?)',(secrets.token_hex(16),actor,server.now(),'rotate scheduled import key',actor))
    return {'token':token}

def valid_sync_key(server,header):
    if not header or not header.startswith('Bearer '):return False
    with server.connect() as con:row=con.execute("SELECT value FROM settings WHERE key='incoming_sync_key'").fetchone()
    return bool(row and hmac.compare_digest(row['value'],digest(header[7:])))
