"""Editable lookup records imported from the workbook's validation tabs."""
import json, uuid, re
from pathlib import Path

_source_path = Path(__file__).with_name('validation-source.json')
SOURCE = json.loads(_source_path.read_text(encoding='utf-8')) if _source_path.exists() else {'importedAt':'','cs':[], 'concerns':[]}
SCHEMAS = {
 'clients': {'title':'Clients','sheet':'CS Data Validation','columns':'T:X','fields':[['name','Client name · T'],['parent','Mother/Hauler → Sub-Client (D) · U'],['serviceType','Managed / Outright · V'],['category','Client category (E) · W'],['code','Client code (Z) · X'],['masterParent','Mother/Hauler · Masterlist'],['keyAccount','Key account · Masterlist']], 'required':['name']},
 'categories': {'title':'Concern mappings','sheet':'CS Data Validation','columns':'L:P','fields':[['name','Detailed category (I) · O'],['concern','Concern category (J) · M'],['severity','Severity (V) · N'],['general','General concerns (X) · L'],['detail','Detailed concern (Y) · P']], 'required':['name','concern','severity','general']},
 'staff': {'title':'Assignees & departments','sheet':'CS Data Validation','columns':'H:I','fields':[['name','Assigned to · I'],['department','Department (R) · H']], 'required':['name','department']},
 'statuses': {'title':'Statuses','sheet':'CS Data Validation','columns':'C:D','fields':[['name','Status · C'],['alert','Alert level · D'],['behavior','App behavior']], 'required':['name','behavior']},
 'concernGroups': {'title':'Concern Category VALIDATION','sheet':'Concern Category VALIDATION','columns':'A:N','fields':[['name','Concern category'],['items','Detailed categories · one per line']], 'required':['name']},
}
for kind,title,column in [('sources','Ticket sources','R'),('directions','Inbound / Outbound','Q'),('severities','Severity options','E'),('sla','SLA labels','F'),('alerts','Alert levels','G'),('clientCategories','Client categories','J'),('actions','Follow-up actions','A'),('designations','Designations','S')]:
    SCHEMAS[kind]={'title':title,'sheet':'CS Data Validation','columns':column,'fields':[['name',title]],'required':['name']}

def norm(value): return re.sub(r'\s+',' ',value.strip()).casefold()

class LookupList(list):
    def __init__(self,values):
        super().__init__(values)
        self.by_name={}
        for r in values: self.by_name.setdefault(norm(r['name']),r)

def seed(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS validation_records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS validation_audit(id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL, before_data TEXT, after_data TEXT NOT NULL);
    ''')
    if con.execute("SELECT 1 FROM settings WHERE key='validation_import'").fetchone(): return
    rows=SOURCE['cs'][1:]
    parents={}; accounts={}
    for r in (SOURCE.get('masterActive') or [])+(SOURCE.get('masterTerminated') or []):
        if r and r[0]: parents.setdefault(norm(r[0]),r[1] if len(r)>1 else '')
    for r in SOURCE.get('masterAccounts') or []:
        if r and r[0]: accounts.setdefault(norm(r[0]),r[2] if len(r)>2 else '')
    def add(kind, data, pos):
        record_id=f'{kind}-{pos}'
        con.execute('INSERT INTO validation_records(id,kind,data,position) VALUES(?,?,?,?)',(record_id,kind,json.dumps(data),pos))
    for i,raw in enumerate(rows,2):
        r=[x or '' for x in raw]+['']*57
        if r[19]: add('clients',dict(name=r[19],parent=r[20],serviceType=r[21],category=r[22],code=r[23],masterParent=parents.get(norm(r[19]),''),keyAccount=accounts.get(norm(r[19]),''),sourceRow=i),i)
        if r[14]: add('categories',dict(name=r[14],concern=r[12],severity=r[13],general=r[11],detail=r[15],sourceRow=i),i)
        if r[8]: add('staff',dict(name=r[8],department=r[7],sourceRow=i),i)
        if r[2]: add('statuses',dict(name=r[2],alert=r[3],behavior='closed' if r[2] in ('Resolved','Temporarily Closed') else 'hold' if r[2]=='On Hold' else 'process' if r[2] in ('On Process',"Waiting Client's Feedback",'Approved') else 'open',sourceRow=i),i)
        for kind,col in [('sources',17),('directions',16),('severities',4),('sla',5),('alerts',6),('clientCategories',9),('actions',0),('designations',18)]:
            if r[col]: add(kind,dict(name=r[col],sourceRow=i),i)
    group_rows=SOURCE['concerns']
    for i,name in enumerate(group_rows[0]):
        add('concernGroups',dict(name=name,items=[r[i] for r in group_rows[1:] if len(r)>i and r[i]],sourceColumn=chr(65+i)),i)
    con.execute("INSERT INTO settings VALUES('validation_import',?)",(SOURCE['importedAt'],))

def records(con):
    result={kind:[] for kind in SCHEMAS}
    for r in con.execute('SELECT * FROM validation_records ORDER BY position,id'):
        result[r['kind']].append({**json.loads(r['data']),'recordId':r['id'],'version':r['version']})
    return result

def config(con):
    result=records(con)
    result['statusRules']={r['name']:r['behavior'] for r in result['statuses']}
    for kind in SCHEMAS:
        if kind not in ('clients','categories','staff','concernGroups'): result[kind]=list(dict.fromkeys(r['name'] for r in result[kind]))
        else: result[kind]=LookupList(result[kind])
    return result

def find(items,name):
    if isinstance(items,LookupList): return items.by_name.get(norm(str(name or '')))
    return next((r for r in items if norm(r['name'])==norm(str(name or ''))),None)

def derive(t,cfg):
    client=find(cfg['clients'],t.get('client'))
    category=find(cfg['categories'],t.get('category'))
    staff=find(cfg['staff'],t.get('assigned'))
    if client:
        t.update(subClient=client['parent'],clientCategory=client['category'],clientCode=client['code'],masterlistCheck=client['masterParent'],keyAccount=client['keyAccount'])
    if category:
        t.update(concern=category['concern'],severity=category['severity'],general=category['general'],detailedConcern=category['detail'])
    if staff: t['department']=staff['department']
    return t

def update(con,payload,actor,stamp):
    kind=payload.get('kind')
    if kind not in SCHEMAS: raise ValueError('Unknown validation list.')
    schema=SCHEMAS[kind]; old=None
    if payload.get('recordId'):
        old=con.execute('SELECT * FROM validation_records WHERE id=? AND kind=?',(payload['recordId'],kind)).fetchone()
        if not old: raise ValueError('Lookup record not found.')
        if old['version']!=payload.get('version'): raise ValueError('This lookup changed. Reopen it before saving.')
    data=json.loads(old['data']) if old else {}
    for key,_ in schema['fields']:
        value=payload.get('values',{}).get(key,'')
        if not isinstance(value,str) or len(value)>20000: raise ValueError('Invalid lookup value.')
        data[key]=value.strip()
    for key in schema['required']:
        if not data.get(key): raise ValueError('Please complete the required fields.')
    if kind=='statuses' and data['behavior'] not in ('open','process','hold','closed'): raise ValueError('Choose a valid status behavior.')
    if kind=='concernGroups':
        data['items']=[line.strip() for line in data['items'].splitlines() if line.strip()]
        if len({norm(s) for s in data['items']})!=len(data['items']): raise ValueError('Remove duplicate detailed categories from this group.')
    old_data=json.loads(old['data']) if old else None
    # Preserve imported duplicates but prevent new ambiguous keys.
    if not old or norm(old_data['name'])!=norm(data['name']):
        for r in records(con)[kind]:
            if norm(r['name'])==norm(data['name']): raise ValueError('That name already exists in this list.')
    record_id=old['id'] if old else kind+'-'+uuid.uuid4().hex
    def write(rid,k,new,before=None):
        if before is not None:
            con.execute('UPDATE validation_records SET data=?,version=version+1 WHERE id=?',(json.dumps(new),rid))
        else:
            position=con.execute('SELECT coalesce(max(position),0)+1 FROM validation_records WHERE kind=?',(k,)).fetchone()[0]
            con.execute('INSERT INTO validation_records(id,kind,data,position) VALUES(?,?,?,?)',(rid,k,json.dumps(new),position))
        con.execute('INSERT INTO validation_audit(record_id,actor,at,before_data,after_data) VALUES(?,?,?,?,?)',(rid,actor,stamp,json.dumps(before) if before else None,json.dumps(new)))
    write(record_id,kind,data,old_data)
    # A group rename updates the corresponding category mapping, without inventing
    # missing severities or general concerns for unconfigured list entries.
    if kind=='concernGroups' and old_data and data['name']!=old_data['name']:
        for r in con.execute("SELECT * FROM validation_records WHERE kind='categories'").fetchall():
            before=json.loads(r['data'])
            if norm(before['concern'])==norm(old_data['name']):
                write(r['id'],'categories',{**before,'concern':data['name']},before)
    # Keep existing group entries aligned when a mapped detailed category is renamed.
    if kind=='categories' and old_data and data['name']!=old_data['name']:
        for r in con.execute("SELECT * FROM validation_records WHERE kind='concernGroups'").fetchall():
            before=json.loads(r['data']); after={**before,'items':[data['name'] if norm(s)==norm(old_data['name']) else s for s in before['items']]}
            if before!=after: write(r['id'],'concernGroups',after,before)
    return record_id,old_data,data

def diagnostics(data):
    duplicates=[]; first={}
    for r in data['clients']:
        key=norm(r['name'])
        if key in first: duplicates.append(r['name'])
        else:first[key]=r
    names={norm(r['name']) for r in data['categories']}
    unmapped=[{'group':g['name'],'name':n} for g in data['concernGroups'] for n in g['items'] if norm(n) not in names]
    return {'duplicateClients':duplicates,'unmappedConcerns':unmapped}
