"""CS Desk local prototype. Python 3.11+, standard library only."""
import csv, io, json, os, secrets, sqlite3, threading, time, uuid, sys
from datetime import datetime, date
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import validation
import sheets_import
import staff_auth

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('CS_DB_PATH', str(ROOT / 'tickets.sqlite3')))
PORT = int(os.environ.get('PORT', os.environ.get('CS_PORT', '8765')))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL and os.environ.get('PGHOST'):
    from psycopg.conninfo import make_conninfo
    DATABASE_URL = make_conninfo(host=os.environ['PGHOST'], port=os.environ.get('PGPORT','5432'),
        dbname=os.environ.get('PGDATABASE','postgres'), user=os.environ.get('PGUSER','postgres'),
        password=os.environ.get('PGPASSWORD',''), sslmode='require')
PUBLIC_ORIGIN = os.environ.get('CS_PUBLIC_ORIGIN', os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
HOST = '0.0.0.0' if PUBLIC_ORIGIN else '127.0.0.1'
TOKEN = secrets.token_urlsafe(32)
SYNC_LOCK = threading.Lock()
SOURCE = json.loads((ROOT / ('source.json' if (ROOT/'source.json').exists() else 'bootstrap.json')).read_text(encoding='utf-8'))
CONFIG = SOURCE['config']
HEADERS = SOURCE['headers']
FIELDS = ['id','month','client','subClient','clientCategory','opened','responded','openTime','category','concern','source','direction','details','latestNotes','cs','status','assigned','department','closed','aging','ticketStatus','severity','agingCategory','general','detailedConcern','clientCode','status2','parentTicket','plate','parentTicket2','createdBy','ticketAging','aging2','closeTime','closeChecker','closedBy','masterlistCheck','timeCheck','keyAccount']
CLOSED = {'Resolved', 'Temporarily Closed'}

class Connection(sqlite3.Connection):
    def __exit__(self, kind, value, traceback):
        try: return super().__exit__(kind, value, traceback)
        finally: self.close()

def connect():
    if DATABASE_URL:
        from postgres import Connection as PostgresConnection
        return PostgresConnection(DATABASE_URL)
    con = sqlite3.connect(DB, timeout=30, factory=Connection)
    con.row_factory = sqlite3.Row
    return con

def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def parse_date(value):
    if not value: return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%Y %I:%M:%S %p'):
        try: return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError): pass
    return None

def initialize():
    if PUBLIC_ORIGIN:
        if not PUBLIC_ORIGIN.startswith('https://') or not DATABASE_URL or not staff_auth.configured():
            raise RuntimeError('Hosted mode requires HTTPS origin, DATABASE_URL and configured Supabase staff authentication.')
    if DATABASE_URL:
        with connect() as con:
            if not con.execute("SELECT 1 FROM settings WHERE key='imported'").fetchone():
                raise RuntimeError('Run migrate_postgres.py before starting the hosted app.')
    with connect() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, synced INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        if not con.execute("SELECT 1 FROM settings WHERE key='imported'").fetchone():
            for row in SOURCE['rows']:
                values = row['values']
                ticket = {key: (values[i] or '') if i < len(values) else '' for i,key in enumerate(FIELDS)}
                ticket['sourceRow'] = row['sourceRow']
                for key in ('opened','responded','closed'):
                    parsed = parse_date(ticket[key])
                    if parsed: ticket[key] = parsed.isoformat()
                con.execute('INSERT INTO tickets(id,data,updated) VALUES(?,?,?)', (ticket['id'],json.dumps(ticket),SOURCE['importedAt']))
            con.execute("INSERT INTO settings VALUES('imported',?)", (SOURCE['importedAt'],))
        if not con.execute("SELECT 1 FROM settings WHERE key='import_notes'").fetchone():
            for row in SOURCE['rows']:
                values=row['values']
                if len(values)>13 and values[13]:
                    con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(values[0],'Google Sheets import',SOURCE['importedAt'],'imported note',values[13]))
            con.execute("INSERT INTO settings VALUES('import_notes','done')")
    with connect() as con:
        validation.seed(con)

def get_config():
    with connect() as con: return validation.config(con)

def decorate(row, cfg=None):
    cfg=cfg or get_config()
    t = json.loads(row['data'])
    behavior=cfg['statusRules'].get(t.get('status'),'open')
    start = parse_date(t.get('opened'))
    end = parse_date(t.get('closed')) if behavior=='closed' else date.today()
    aging = max(0, ((end or date.today()) - start).days) if start else None
    t['aging'] = aging
    t['agingCategory'] = '' if aging is None else ('Within 1 day' if aging <= 1 else 'Within 2 days' if aging == 2 else 'Within 3 days' if aging == 3 else 'Within 7 days' if aging <= 7 else 'More than 7 days')
    t['ticketStatus'] = {'closed':'Closed','hold':'On Hold','process':'In Process','open':'Open Tickets'}[behavior]
    t['status2'] = 'Resolved' if behavior=='closed' else 'On Hold' if behavior=='hold' else 'Pending'
    t['aging2'] = t['agingCategory']
    t['ticketAging'] = t['id'] + (' - ' + str(aging) + ' Days' if aging is not None else '')
    if start: t['month']=start.strftime('%m %B')
    t['closeChecker']='No Time Input' if t.get('closed') and not t.get('closeTime') else ''
    t['timeCheck']='No Time Input' if t.get('client') and not t.get('openTime') else ''
    t.update(version=row['version'], synced=row['synced'] == row['version'], updated=row['updated'])
    return t

def all_tickets():
    with connect() as con:
        cfg=validation.config(con)
        return [decorate(r,cfg) for r in con.execute('SELECT * FROM tickets ORDER BY rowid DESC')]

def detail(ticket_id):
    with connect() as con:
        row = con.execute('SELECT * FROM tickets WHERE id=?',(ticket_id,)).fetchone()
        if not row: raise ValueError('Ticket not found')
        t = decorate(row)
        t['events'] = [dict(r) for r in con.execute('SELECT actor,at,kind,body FROM events WHERE ticket_id=? ORDER BY event_id DESC',(ticket_id,))]
        return t

def require(value, message):
    if not value: raise ValueError(message)

def save(payload):
    actor = str(payload.get('actor','')).strip()
    stamp = now()
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        CONFIG=validation.config(con)
        require(actor in [s['name'] for s in CONFIG['staff']] or (bool(PUBLIC_ORIGIN) and staff_auth.approved(actor)), 'Choose a CS operator from the list.')
        if payload.get('id'):
            row = con.execute('SELECT * FROM tickets WHERE id=?',(payload['id'],)).fetchone()
            require(row, 'Ticket not found')
            require(row['version'] == payload.get('version'), 'This ticket changed. Reopen it before saving.')
            t = json.loads(row['data'])
            status = payload.get('status',t['status'])
            assigned = payload.get('assigned', t['assigned'])
            require(status in CONFIG['statuses'], 'Choose a valid status.')
            require(assigned in [s['name'] for s in CONFIG['staff']], 'Choose a valid assignee.')
            note = str(payload.get('note','')).strip()
            require(len(note) <= 10000, 'Note is too long.')
            changes = []
            if status != t['status']:
                changes.append(f"Status: {t['status']} → {status}")
                if CONFIG['statusRules'][status]=='closed':
                    t.update(closed=date.today().isoformat(), closeTime=datetime.now().strftime('%I:%M %p'), closedBy=actor)
                elif CONFIG['statusRules'].get(t['status'])=='closed':
                    t.update(closed='', closeTime='', closedBy='')
                t['status'] = status
            if assigned != t['assigned']:
                changes.append(f"Assigned: {t['assigned']} → {assigned}")
                t['assigned'] = assigned
                t['department'] = next(s['department'] for s in CONFIG['staff'] if s['name']==assigned)
            require(note or changes, 'Add a note or change the status or assignee.')
            if note:
                t['latestNotes'] = note
                con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(t['id'],actor,stamp,'note',note))
            if changes:
                con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(t['id'],actor,stamp,'change','; '.join(changes)))
            validation.derive(t,CONFIG)
            con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(t),stamp,t['id']))
        else:
            client = validation.find(CONFIG['clients'],payload.get('client'))
            category = validation.find(CONFIG['categories'],payload.get('category'))
            require(client, 'Select a client from the master list.')
            require(category, 'Select a valid detailed category.')
            require(category['concern'] and category['severity'] and category['general'], 'Complete this concern mapping in CS Data Validation first.')
            require(payload.get('source') in CONFIG['sources'], 'Select a valid ticket source.')
            require(payload.get('direction') in CONFIG['directions'], 'Select Inbound or Outbound.')
            assigned = payload.get('assigned')
            require(assigned in [s['name'] for s in CONFIG['staff']], 'Select an assignee.')
            description = str(payload.get('details','')).strip()
            require(description and len(description)<=10000, 'Enter ticket details (up to 10,000 characters).')
            parent = str(payload.get('parentTicket','')).strip()
            if parent: require(con.execute('SELECT 1 FROM tickets WHERE id=?',(parent,)).fetchone(), 'Parent ticket was not found.')
            ticket_id = 'CSAPP-' + str(date.today().year) + '-' + uuid.uuid4().hex[:12].upper()
            t = dict.fromkeys(FIELDS,'')
            t.update(id=ticket_id, client=client['name'], subClient=client['parent'],clientCategory=client['category'],clientCode=client['code'],category=category['name'],concern=category['concern'],severity=category['severity'],general=category['general'],detailedConcern=category['detail'],source=payload['source'],direction=payload['direction'],details=description,assigned=assigned,cs=actor,createdBy=actor,status='Assigned',opened=date.today().isoformat(),openTime=datetime.now().strftime('%I:%M %p'),month=datetime.now().strftime('%m %B'),department=next(s['department'] for s in CONFIG['staff'] if s['name']==assigned),parentTicket=parent,plate=str(payload.get('plate','')).strip()[:100])
            t['status']='Assigned' if 'Assigned' in CONFIG['statuses'] else next(s for s in CONFIG['statuses'] if CONFIG['statusRules'][s]=='open')
            t['parentTicket']=parent or t['id']
            t['parentTicket2']=parent or t['id']
            validation.derive(t,CONFIG)
            con.execute('INSERT INTO tickets(id,data,updated) VALUES(?,?,?)',(t['id'],json.dumps(t),stamp))
            con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(t['id'],actor,stamp,'created','Ticket created in CS Desk'))
    return detail(t['id'])

def save_validation(payload):
    actor=str(payload.get('actor','')).strip(); stamp=now(); affected=0
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        before_cfg=validation.config(con)
        require(actor in [r['name'] for r in before_cfg['staff']] or (bool(PUBLIC_ORIGIN) and staff_auth.approved(actor)), 'Choose your operator name before editing validation data.')
        record_id,old,new=validation.update(con,payload,actor,stamp)
        kind=payload['kind']; after_cfg=validation.config(con)
        require(any(v=='open' for v in after_cfg['statusRules'].values()),'Keep at least one open status.')
        rows=con.execute('SELECT * FROM tickets').fetchall()
        for row in rows:
            t=json.loads(row['data']); previous=dict(t); applies=False
            base={'clients':'client','categories':'category','staff':'assigned','statuses':'status','sources':'source','directions':'direction'}.get(kind)
            if base:
                if kind in ('clients','categories','staff'):
                    selected=validation.find(before_cfg[kind],t.get(base))
                    applies=(selected and selected['recordId']==record_id) or (not selected and validation.norm(str(t.get(base,'')))==validation.norm(new['name']))
                else: applies=validation.norm(str(t.get(base,'')))==validation.norm((old or new)['name'])
                if applies:
                    if kind=='statuses' and old and old['behavior']!=new['behavior']:
                        raise ValueError('This status is in use. Add a new status to change its open/closed behavior.')
                    t[base]=new['name']
            elif kind=='concernGroups' and old and old['name']!=new['name']:
                applies=validation.norm(str(t.get('concern','')))==validation.norm(old['name'])
            if not applies: continue
            validation.derive(t,after_cfg)
            if t!=previous:
                affected+=1
                con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(t),stamp,t['id']))
                changes={k:{'before':previous.get(k,''),'after':v} for k,v in t.items() if v!=previous.get(k)}
                con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(t['id'],actor,stamp,'lookup update',json.dumps(changes,ensure_ascii=False)))
    return {'recordId':record_id,'affectedTickets':affected,'message':'Validation saved to the local database.'}

def sync_batch():
    endpoint=os.environ.get('CS_SHEETS_URL','')
    secret=os.environ.get('CS_SHEETS_SECRET','')
    require(endpoint.startswith('https://script.google.com/macros/s/') and secret, 'Google Sheets sync is not configured. See SETUP.md.')
    require(SYNC_LOCK.acquire(blocking=False), 'A sync is already running.')
    try:
        with connect() as con:
            rows=con.execute('SELECT * FROM tickets WHERE version != synced ORDER BY rowid LIMIT 200').fetchall()
        if not rows: return {'sent':0,'remaining':0}
        cfg=get_config(); items=[]
        for r in rows:
            ticket=decorate(r,cfg)
            items.append({'id':r['id'],'version':r['version'],'values':[ticket.get(k,'') for k in FIELDS]})
        payload={'secret':secret,'tickets':items}
        req=Request(endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=60) as response: result=json.load(response)
        require(result.get('ok') and set(result.get('ids',[])) == {r['id'] for r in rows}, 'Google Sheets did not acknowledge this batch; it remains queued.')
        with connect() as con:
            for r in rows: con.execute('UPDATE tickets SET synced=? WHERE id=? AND version=?',(r['version'],r['id'],r['version']))
            remaining=con.execute('SELECT count(*) FROM tickets WHERE version != synced').fetchone()[0]
        return {'sent':len(rows),'remaining':remaining}
    finally: SYNC_LOCK.release()

def sync_worker():
    while True:
        delay=30
        if os.environ.get('CS_SHEETS_URL') and os.environ.get('CS_SHEETS_SECRET'):
            try:
                result=sync_batch()
                if result['remaining']: delay=2
            except Exception:
                pass  # Unacknowledged versions stay queued for retry.
        time.sleep(delay)

class Handler(BaseHTTPRequestHandler):
    def respond(self, data, status=200, content_type='application/json', cookie=None):
        body=json.dumps(data,ensure_ascii=False).encode() if content_type=='application/json' else data
        self.send_response(status)
        self.send_header('Content-Type',content_type+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('X-Frame-Options','DENY')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(body)
    def allowed(self):
        return self.headers.get('Host') in {f'127.0.0.1:{PORT}', f'localhost:{PORT}', urlparse(PUBLIC_ORIGIN).netloc}
    def do_GET(self):
        if not self.allowed(): return self.respond({'error':'Invalid host'},403)
        parsed=urlparse(self.path); q=parse_qs(parsed.query)
        user = staff_auth.current(self.headers.get('Cookie')) if PUBLIC_ORIGIN and parsed.path!='/healthz' else None
        if PUBLIC_ORIGIN and parsed.path!='/healthz' and not user:
            if parsed.path in ('/','/login'): return self.respond((ROOT/'login.html').read_bytes(),content_type='text/html')
            return self.respond({'error':'Please sign in with an approved staff account.'},401)
        try:
            if parsed.path=='/healthz':
                with connect() as con: con.execute('SELECT 1').fetchone()
                return self.respond({'ok':True})
            if parsed.path=='/': return self.respond((ROOT/'index.html').read_bytes(),content_type='text/html')
            if parsed.path=='/validation-ui.js': return self.respond((ROOT/'validation-ui.js').read_bytes(),content_type='text/javascript')
            if parsed.path=='/validation.css': return self.respond((ROOT/'validation.css').read_bytes(),content_type='text/css')
            if parsed.path=='/api/config': return self.respond({**get_config(),'token':TOKEN,'currentUser':user,'importedAt':SOURCE['importedAt'],'importedCount':len(all_tickets()),'syncConfigured':bool(os.environ.get('CS_SHEETS_URL') and os.environ.get('CS_SHEETS_SECRET'))})
            if parsed.path=='/api/validation':
                with connect() as con: data=validation.records(con)
                return self.respond({'records':data,'schemas':validation.SCHEMAS,'diagnostics':validation.diagnostics(data),'importedAt':validation.SOURCE['importedAt']})
            if parsed.path=='/api/validation/history':
                with connect() as con:
                    history=[dict(r) for r in con.execute('SELECT actor,at,before_data,after_data FROM validation_audit WHERE record_id=? ORDER BY id DESC LIMIT 20',(q.get('id',[''])[0],))]
                return self.respond(history)
            if parsed.path=='/api/sheet-sync': return self.respond(sheets_import.status(sys.modules[__name__]))
            if parsed.path=='/api/sheet-conflicts':
                with connect() as con:
                    sheets_import.setup(con)
                    conflicts=[dict(r) for r in con.execute('SELECT * FROM sheet_conflicts ORDER BY at DESC LIMIT 100')]
                return self.respond(conflicts)
            if parsed.path=='/api/ticket': return self.respond(detail(q.get('id',[''])[0]))
            if parsed.path in ('/api/tickets','/api/export'):
                tickets=all_tickets()
                if parsed.path=='/api/export':
                    out=io.StringIO(); writer=csv.writer(out); writer.writerow(HEADERS)
                    for t in tickets:
                        values=[t.get(k,'') for k in FIELDS]
                        writer.writerow([("'"+v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
                    return self.respond(('\ufeff'+out.getvalue()).encode(),content_type='text/csv')
                active=[t for t in tickets if t['ticketStatus']!='Closed']
                stats={'total':len(tickets),'open':len(active),'aged':sum((t['aging'] or 0)>7 for t in active),'critical':sum(t['severity']=='Critical' for t in active),'queued':sum(not t['synced'] for t in tickets)}
                search=q.get('q',[''])[0].lower(); status=q.get('status',[''])[0]; owner=q.get('owner',[''])[0]; view=q.get('view',['all'])[0]
                client=q.get('client',[''])[0]; subclient=q.get('subclient',[''])[0]; ticket_query=q.get('ticket',[''])[0].strip().casefold()
                filter_options={'clients':sorted({t['client'] for t in tickets if t.get('client')}),'subclients':sorted({t['subClient'] for t in tickets if t.get('subClient')})}
                filtered=[t for t in tickets if (not search or search in ' '.join(str(t.get(k,'')) for k in ['id','client','details','plate','category']).lower()) and (not status or t['status']==status) and (not owner or t['assigned']==owner) and (view!='open' or t['ticketStatus']!='Closed') and (view!='aged' or t['ticketStatus']!='Closed' and (t['aging'] or 0)>7)]
                filtered=[t for t in filtered if (not client or t.get('client')==client) and (not subclient or t.get('subClient')==subclient) and (not ticket_query or any(ticket_query in str(t.get(k,'')).casefold() for k in ('id','parentTicket2')))]
                client_matches=[list(pair) for pair in sorted({(t.get('client',''),t.get('subClient','')) for t in filtered})]
                page=max(1,int(q.get('page',['1'])[0])); start=(page-1)*40
                return self.respond({'tickets':filtered[start:start+40],'count':len(filtered),'stats':stats,'page':page,'filterOptions':filter_options,'clientMatches':client_matches})
            return self.respond({'error':'Not found'},404)
        except ValueError as e: return self.respond({'error':str(e)},400)
    def do_POST(self):
        if not self.allowed(): return self.respond({'error':'Invalid host'},403)
        if PUBLIC_ORIGIN and self.headers.get('Origin') != PUBLIC_ORIGIN:
            return self.respond({'error':'Invalid origin'},403)
        if PUBLIC_ORIGIN and self.path=='/auth/login':
            try:
                size=int(self.headers.get('Content-Length','0'))
                require(0<size<=8192,'Invalid request size')
                body=json.loads(self.rfile.read(size))
                cookie=staff_auth.login(str(body.get('email','')).strip().lower(),str(body.get('password','')))
                return self.respond({'ok':True},cookie=cookie)
            except (ValueError,TypeError,AttributeError):
                return self.respond({'error':'Sign-in failed. Check your account or wait one minute before retrying.'},401)
        if PUBLIC_ORIGIN and self.path=='/auth/logout':
            return self.respond({'ok':True},cookie=staff_auth.logout_cookie())
        user=staff_auth.current(self.headers.get('Cookie')) if PUBLIC_ORIGIN else None
        if PUBLIC_ORIGIN and not user: return self.respond({'error':'Please sign in again.'},401)
        if self.headers.get('X-CS-Token')!=TOKEN: return self.respond({'error':'Reload this app before saving.'},403)
        try:
            size=int(self.headers.get('Content-Length','0')); require(0<size<=50000,'Invalid request size')
            payload=json.loads(self.rfile.read(size))
            if user: payload['actor']=user
            if self.path=='/api/ticket': return self.respond(save(payload))
            if self.path=='/api/validation': return self.respond(save_validation(payload))
            if self.path=='/api/sheet-conflict': return self.respond(sheets_import.resolve(sys.modules[__name__],payload))
            if self.path=='/api/sync': return self.respond(sync_batch())
            return self.respond({'error':'Not found'},404)
        except (ValueError, KeyError, TypeError) as e: return self.respond({'error':str(e)},400)
        except Exception: return self.respond({'error':'The operation could not finish. Your saved tickets remain in the database; retry or check the connection.'},500)
    def log_message(self,*args): pass

if __name__=='__main__':
    initialize()
    threading.Thread(target=sync_worker,daemon=True).start()
    print(f'CS Desk: http://127.0.0.1:{PORT} — local prototype',flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
