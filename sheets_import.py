"""Merge a connector-read workbook snapshot into CS Desk without losing app edits."""
import hashlib, json, sqlite3, sys
from pathlib import Path

DIRECT = ['client','opened','responded','openTime','category','source','direction','details','latestNotes','cs','status','assigned','closed','parentTicket','plate','createdBy','closeTime','closedBy']

def setup(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS sheet_baselines(key TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sheet_conflicts(key TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT NOT NULL, local_value TEXT NOT NULL, sheet_value TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sheet_sync_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')

def lookup_snapshot(server, snapshot):
    previous=server.validation.SOURCE
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    try:
        server.validation.SOURCE=snapshot
        con.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        server.validation.seed(con)
        return server.validation.records(con)
    finally:
        server.validation.SOURCE=previous;con.close()

def keyed_lookups(server,data):
    for kind,items in data.items():
        occurrences={}
        for r in items:
            name=server.validation.norm(r['name']);occurrences[name]=occurrences.get(name,0)+1
            key='lookup:'+json.dumps([kind,name,occurrences[name]],ensure_ascii=False)
            yield key,kind,r

def ticket(server,values,row):
    t={key:(values[i] or '') if i<len(values) else '' for i,key in enumerate(server.FIELDS)}
    t['sourceRow']=row
    for key in ('opened','responded','closed'):
        d=server.parse_date(t[key])
        if d:t[key]=d.isoformat()
    return t

def baselines(server,con):
    if con.execute("SELECT 1 FROM sheet_sync_state WHERE key='baseline_ready'").fetchone():return
    for r in server.SOURCE['rows']:
        t=ticket(server,r['values'],r['sourceRow'])
        con.execute('INSERT INTO sheet_baselines VALUES(?,?,?,?) ON CONFLICT(key) DO NOTHING',('ticket:'+t['id'],'ticket',t['id'],json.dumps(t)))
    original=lookup_snapshot(server,server.validation.SOURCE)
    for key,kind,r in keyed_lookups(server,original):
        data={k:v for k,v in r.items() if k not in ('recordId','version')}
        con.execute('INSERT INTO sheet_baselines VALUES(?,?,?,?) ON CONFLICT(key) DO NOTHING',(key,'lookup',r['recordId'],json.dumps(data)))
    con.execute("INSERT INTO sheet_sync_state VALUES('baseline_ready','true')")

def merge(con,key,entity_type,entity_id,current,base,incoming,fields,stamp):
    result=dict(current)
    for field in fields:
        remote=incoming.get(field,'');local=current.get(field,'');old=base.get(field,'')
        conflict_key=key+':'+field
        if remote==local:
            con.execute('DELETE FROM sheet_conflicts WHERE key=?',(conflict_key,));continue
        if remote==old:
            con.execute('UPDATE sheet_conflicts SET local_value=?,sheet_value=?,at=? WHERE key=?',(json.dumps(local),json.dumps(remote),stamp,conflict_key))
            continue
        if local==old:
            result[field]=remote
            con.execute('DELETE FROM sheet_conflicts WHERE key=?',(conflict_key,))
        else:
            con.execute('INSERT INTO sheet_conflicts VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET entity_type=excluded.entity_type,entity_id=excluded.entity_id,field=excluded.field,local_value=excluded.local_value,sheet_value=excluded.sheet_value,at=excluded.at',(conflict_key,entity_type,entity_id,field,json.dumps(local),json.dumps(remote),stamp))
    return result

def apply_snapshot(server,snapshot):
    if snapshot.get('spreadsheetId')!=server.SOURCE['spreadsheetId']:raise ValueError('Unexpected spreadsheet ID')
    if not isinstance(snapshot.get('ticketRows'),list):raise ValueError('Missing ticket rows')
    for name in ('cs','concerns','masterActive','masterTerminated','masterAccounts'):
        if name not in snapshot:raise ValueError('Incomplete workbook snapshot: '+name)
    if not snapshot['cs'] or not snapshot['concerns']:raise ValueError('Validation reads were empty; no changes applied')
    desired=lookup_snapshot(server,snapshot);stamp=server.now();added=updated=lookup_changes=0
    with server.connect() as con:
        setup(con);con.execute('BEGIN IMMEDIATE');baselines(server,con)
        for key,kind,r in keyed_lookups(server,desired):
            incoming={k:v for k,v in r.items() if k not in ('recordId','version')}
            baseline=con.execute('SELECT * FROM sheet_baselines WHERE key=?',(key,)).fetchone()
            current_row=con.execute('SELECT * FROM validation_records WHERE id=?',(baseline['entity_id'],)).fetchone() if baseline else None
            if not current_row and not baseline:
                # An app-created row with the same name is reconciled, not duplicated.
                candidates=server.validation.records(con)[kind]
                candidate=server.validation.find(candidates,incoming['name'])
                if candidate:current_row=con.execute('SELECT * FROM validation_records WHERE id=?',(candidate['recordId'],)).fetchone()
            if current_row:
                entity_id=current_row['id'];before=json.loads(current_row['data']);base=json.loads(baseline['data']) if baseline else {}
                after=merge(con,key,'lookup',entity_id,before,base,incoming,[f for f,_ in server.validation.SCHEMAS[kind]['fields']],stamp)
                if after!=before:
                    con.execute('UPDATE validation_records SET data=?,version=version+1 WHERE id=?',(json.dumps(after),entity_id))
                    con.execute('INSERT INTO validation_audit(record_id,actor,at,before_data,after_data) VALUES(?,?,?,?,?)',(entity_id,'Google Sheets sync',stamp,json.dumps(before),json.dumps(after)))
                    lookup_changes+=1
            else:
                entity_id='sheet-'+kind+'-'+hashlib.sha256(key.encode()).hexdigest()[:20]
                position=con.execute('SELECT coalesce(max(position),0)+1 FROM validation_records WHERE kind=?',(kind,)).fetchone()[0]
                con.execute('INSERT INTO validation_records(id,kind,data,position) VALUES(?,?,?,?)',(entity_id,kind,json.dumps(incoming),position));lookup_changes+=1
            con.execute('INSERT INTO sheet_baselines VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET entity_type=excluded.entity_type,entity_id=excluded.entity_id,data=excluded.data',(key,'lookup',entity_id,json.dumps(incoming)))
        cfg=server.validation.config(con)
        for row_number,values in enumerate(snapshot['ticketRows'][1:],2):
            if len(values)<3 or not values[2] or not values[0]:continue
            incoming=ticket(server,values,row_number);entity_id=incoming['id'];key='ticket:'+entity_id
            row=con.execute('SELECT * FROM tickets WHERE id=?',(entity_id,)).fetchone()
            baseline=con.execute('SELECT * FROM sheet_baselines WHERE key=?',(key,)).fetchone()
            if not row:
                after=server.validation.derive(dict(incoming),cfg)
                con.execute('INSERT INTO tickets(id,data,updated) VALUES(?,?,?)',(entity_id,json.dumps(after),stamp))
                con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(entity_id,'Google Sheets sync',stamp,'sheet import','New ticket imported from Google Sheets'))
                if incoming['latestNotes']:con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(entity_id,'Google Sheets sync',stamp,'imported note',incoming['latestNotes']))
                added+=1
            else:
                before=json.loads(row['data']);base=json.loads(baseline['data']) if baseline else {}
                after=merge(con,key,'ticket',entity_id,before,base,incoming,DIRECT,stamp)
                after=server.validation.derive(after,cfg)
                if after!=before:
                    con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(after),stamp,entity_id))
                    changes={k:{'before':before.get(k,''),'after':v} for k,v in after.items() if before.get(k)!=v}
                    con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(entity_id,'Google Sheets sync',stamp,'sheet update',json.dumps(changes,ensure_ascii=False)))
                    updated+=1
            con.execute('INSERT INTO sheet_baselines VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET entity_type=excluded.entity_type,entity_id=excluded.entity_id,data=excluded.data',(key,'ticket',entity_id,json.dumps(incoming)))
        # App-created tickets also use the latest imported reference mappings.
        if lookup_changes:
            for row in con.execute('SELECT * FROM tickets').fetchall():
                before=json.loads(row['data']);after=server.validation.derive(dict(before),cfg)
                if after!=before:
                    con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(after),stamp,row['id']))
                    changes={k:{'before':before.get(k,''),'after':v} for k,v in after.items() if before.get(k)!=v}
                    con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(row['id'],'Google Sheets sync',stamp,'lookup update',json.dumps(changes,ensure_ascii=False)));updated+=1
        conflicts=con.execute('SELECT count(*) FROM sheet_conflicts').fetchone()[0]
        result={'checkedAt':stamp,'snapshotAt':snapshot.get('importedAt',stamp),'added':added,'updated':updated,'lookupChanges':lookup_changes,'conflicts':conflicts,'mode':'scheduled','intervalMinutes':15}
        con.execute("INSERT INTO sheet_sync_state VALUES('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(result),))
    return result

def status(server):
    with server.connect() as con:
        setup(con)
        r=con.execute("SELECT value FROM sheet_sync_state WHERE key='latest'").fetchone()
        result=json.loads(r[0]) if r else {'checkedAt':None,'mode':'not yet checked','intervalMinutes':15}
        result['conflicts']=con.execute('SELECT count(*) FROM sheet_conflicts').fetchone()[0]
        return result

def resolve(server,payload):
    with server.connect() as con:
        setup(con);con.execute('BEGIN IMMEDIATE')
        cfg=server.validation.config(con);actor=payload.get('actor','')
        if not (any(r['name']==actor for r in cfg['staff']) or (server.PUBLIC_ORIGIN and server.staff_auth.approved(actor))):raise ValueError('Choose an operator first.')
        conflict=con.execute('SELECT * FROM sheet_conflicts WHERE key=?',(payload.get('key',''),)).fetchone()
        if not conflict:raise ValueError('Conflict already resolved. Refresh the list.')
        if payload.get('choice') not in ('app','sheet'):raise ValueError('Choose the app or sheet value.')
        # The observed values must still match the review that the user saw.
        if payload.get('at')!=conflict['at']:raise ValueError('This conflict changed. Refresh before resolving.')
        stamp=server.now();is_ticket=conflict['entity_type']=='ticket'
        table='tickets' if is_ticket else 'validation_records'
        row=con.execute(f'SELECT * FROM {table} WHERE id=?',(conflict['entity_id'],)).fetchone()
        before=json.loads(row['data']);field=conflict['field']
        if before.get(field,'')!=json.loads(conflict['local_value']):raise ValueError('The app value changed. Wait for the next sheet refresh before resolving.')
        after=dict(before)
        if payload['choice']=='sheet':after[field]=json.loads(conflict['sheet_value'])
        if is_ticket:
            server.validation.derive(after,cfg)
            con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(after),stamp,row['id']))
            con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(row['id'],actor,stamp,'conflict resolved',f"{field}: kept {payload['choice']} value"))
        else:
            con.execute('UPDATE validation_records SET data=?,version=version+1 WHERE id=?',(json.dumps(after),row['id']))
            con.execute('INSERT INTO validation_audit(record_id,actor,at,before_data,after_data) VALUES(?,?,?,?,?)',(row['id'],actor,stamp,json.dumps(before),json.dumps(after)))
            cfg=server.validation.config(con)
            for trow in con.execute('SELECT * FROM tickets').fetchall():
                old=json.loads(trow['data']);new=server.validation.derive(dict(old),cfg)
                if old!=new:
                    con.execute('UPDATE tickets SET data=?,updated=?,version=version+1 WHERE id=?',(json.dumps(new),stamp,trow['id']))
                    con.execute('INSERT INTO events(ticket_id,actor,at,kind,body) VALUES(?,?,?,?,?)',(trow['id'],actor,stamp,'lookup conflict resolved',field+' updated from validation data'))
        con.execute('DELETE FROM sheet_conflicts WHERE key=?',(conflict['key'],))
    return {'ok':True}

if __name__=='__main__':
    import server
    server.initialize()
    if len(sys.argv)!=2:raise SystemExit('Usage: python sheets_import.py SNAPSHOT.json')
    snapshot=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
    print(json.dumps(apply_snapshot(server,snapshot)))
