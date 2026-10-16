#!/usr/bin/env python3
"""Offline SQLite source database and static HTTPS cloud export. Python 3.10+."""
import argparse, datetime, hashlib, json, re, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DB=ROOT/'data/rules.sqlite3'
WARNING='НЕ ПРОВЕРЕНО НА АКТУАЛЬНОСТЬ. Возможны пропуски страниц, изображений и внешних документов. Это справочный текст, не готовая таблица наказаний.'

def dumps(x): return json.dumps(x,ensure_ascii=False,indent=2)+'\n'
def write(p,x): p.parent.mkdir(parents=True,exist_ok=True);p.write_text(dumps(x),encoding='utf-8')
def clean(s):
    s=s.replace('\r\n','\n').replace('\r','\n').replace('\u200b','').replace('\xa0',' ')
    # Remove only valid JSON dictionaries whose keys are all XenForo lightbox labels.
    def strip_ui(match):
        try:
            obj=json.loads(match[0])
            if obj and all(k.startswith('lightbox_') for k in obj):return ''
        except ValueError:pass
        return match[0]
    s=re.sub(r'\{\s*"lightbox_close"[^{}]*\}',strip_ui,s)
    s=re.sub(r'[ \t]+',' ',s)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+','\n\n',s).strip()
def slug(s):
    m=re.search(r'\[([^]]+)\]',s)
    return re.sub('[^a-z0-9]+','-', (m[1] if m else s).lower()).strip('-')
def filename(key): return re.sub(r'[^a-zA-Z0-9_.-]','_',key)
def connect():
    c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def init(source):
    if DB.exists(): raise SystemExit('Database exists. Import into a new directory to preserve bindings/review history.')
    DB.parent.mkdir(exist_ok=True)
    manifest=json.loads((source/'server_sources.json').read_text())
    topics=json.loads((source/'topics.json').read_text())['results']+json.loads((source/'mobile.json').read_text())['results']
    common=json.loads((source/'common.json').read_text())
    c=connect();c.executescript('''
    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE servers(id TEXT PRIMARY KEY,name TEXT NOT NULL,platform TEXT NOT NULL,server_number INTEGER,forum_url TEXT NOT NULL);
    CREATE TABLE documents(id INTEGER PRIMARY KEY,server_id TEXT REFERENCES servers(id),scope TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,captured_at TEXT,has_more_pages INTEGER NOT NULL,review_status TEXT NOT NULL DEFAULT 'unreviewed',links_json TEXT NOT NULL);
    CREATE TABLE posts(document_id INTEGER NOT NULL REFERENCES documents(id),position INTEGER NOT NULL,text TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(document_id,position));
    CREATE TABLE bindings(server_key TEXT PRIMARY KEY,server_id TEXT NOT NULL REFERENCES servers(id),verified_at TEXT NOT NULL);
    CREATE INDEX documents_by_server ON documents(server_id,scope);
    ''')
    c.execute('INSERT INTO metadata VALUES(?,?)',('assembled_at',datetime.datetime.now(datetime.timezone.utc).isoformat()))
    c.execute('INSERT INTO metadata VALUES(?,?)',('warning',WARNING))
    lookup={}
    for s in manifest['servers']:
        ident=slug(s['name']);lookup[s['name']]=ident
        c.execute('INSERT INTO servers VALUES(?,?,?,?,?)',(ident,s['name'],s['platform'],s['serverNumber'],s['forumUrl']))
    scopes={(s['name'],x['url']):x['scope'] for s in manifest['servers'] for x in s.get('sources',[])}
    for t in common+topics:
        if t.get('status',200)!=200: continue
        sid=lookup.get(t.get('server'));scope='common' if not sid else scopes.get((t['server'],t['url']),'shared_or_legislation')
        cur=c.execute('INSERT INTO documents(server_id,scope,title,url,captured_at,has_more_pages,links_json) VALUES(?,?,?,?,?,?,?)',
          (sid,scope,clean(t['title']),t['url'],t.get('checkedAt'),int(bool(t.get('pagination'))),json.dumps(t.get('links',[]),ensure_ascii=False)))
        for pos,text in enumerate(t.get('posts',[t.get('text','')]),1):
            text=clean(text)
            if text: c.execute('INSERT INTO posts VALUES(?,?,?,?)',(cur.lastrowid,pos,text,hashlib.sha256(text.encode()).hexdigest()))
    c.commit();c.close();export()

def documents(c,sid):
    out=[]
    for r in c.execute('SELECT * FROM documents WHERE server_id=? OR server_id IS NULL ORDER BY id',(sid,)):
        d=dict(r);d['has_more_pages']=bool(d['has_more_pages']);d['links']=json.loads(d.pop('links_json'))
        d['posts']=[dict(p) for p in c.execute('SELECT position,text,sha256 FROM posts WHERE document_id=? ORDER BY position',(d['id'],))]
        out.append(d)
    return out

def chunks(text,limit=15000):
    # Split only for renderer size; no legal reinterpretation or dropped tail.
    part=[];size=0
    for char in text:
        n=len(char.encode('utf-8'))
        if size+n>limit: yield ''.join(part);part=[];size=0
        part.append(char);size+=n
    if part:yield ''.join(part)

def compatible(c,sid,key):
    result={'formatVersion':1,'serverKey':key,'serverId':sid,'reviewStatus':'unreviewed','warning':WARNING,'fines':[],'wanted':[]}
    for d in documents(c,sid):
        title=d['title'].lower()
        # Documentary navigation, NOT inference of fines, wanted levels or offences.
        if d['scope']=='common': buckets=['fines','wanted']
        elif re.search(r'административн.*кодекс|правил.*дорожн|\bпдд\b',title): buckets=['fines']
        elif re.search(r'уголовн.*кодекс|единый.*федеральн.*кодекс|\bефк\b',title): buckets=['wanted']
        else:continue
        for p in d['posts']:
            for i,part in enumerate(chunks(p['text']),1):
                prefix=f"{WARNING}\nИсточник: {d['url']}\nДата сохранения: {d['captured_at'] or 'не записана'}\n\n"
                entry={'title':d['title'][:130]+f" · сообщение {p['position']}, часть {i}",'text':prefix+part,'sourceUrl':d['url'],'reviewStatus':'unreviewed'}
                for bucket in buckets:result[bucket].append(entry.copy())
    for bucket in ['fines','wanted']:
        if len(result[bucket])>1000:raise ValueError('Too many entries: '+sid)
        for e in result[bucket]:assert len(e['title'].encode())<=512 and len(e['text'].encode())<=20000
    if len(dumps(result).encode())>4*1024*1024:raise ValueError('Package exceeds 4MB: '+sid)
    result['version']=hashlib.sha256(dumps(result).encode()).hexdigest()
    return result

def export():
    c=connect();pub=ROOT/'public';pub.mkdir(exist_ok=True)
    # Remove obsolete address exports so a changed binding cannot leak old data.
    for p in (pub/'rules').glob('*.json') if (pub/'rules').exists() else []:p.unlink()
    summary=[]
    for s in c.execute('SELECT * FROM servers ORDER BY platform,server_number,id'):
        item=dict(s);docs=documents(c,s['id']);item['documentCount']=len([d for d in docs if d['server_id']])
        item['rulesUrl']=f"servers/{s['id']}.json";summary.append(item)
        write(pub/'servers'/f"{s['id']}.json",{'formatVersion':1,'server':dict(s),'reviewStatus':'unreviewed','warning':WARNING,'documents':docs})
    for b in c.execute('SELECT * FROM bindings'):
        package=compatible(c,b['server_id'],b['server_key'])
        write(pub/'rules'/f"{filename(b['server_key'])}.json",package)
        write(pub/'rules'/f"{filename(b['server_key'])}.version.json",{'formatVersion':1,'serverKey':b['server_key'],'version':package['version']})
    counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['servers','documents','posts','bindings']}
    paginated=c.execute('SELECT count(*) FROM documents WHERE has_more_pages=1').fetchone()[0]
    write(pub/'index.json',{'formatVersion':1,'status':'unreviewed_source_snapshot','warning':WARNING,'counts':counts,'paginatedDocuments':paginated,'servers':summary})
    (pub/'index.html').write_text('''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Arizona Helper — база источников</title><style>body{font:16px system-ui;background:#101720;color:#e6edf5;max-width:900px;margin:40px auto;padding:24px}a{color:#82b9ff}li{margin:10px 0}.warn{padding:20px;border:1px solid #b87d42;border-radius:10px;background:#30271e}</style><h1>Arizona Helper · база источников</h1><p class="warn">Сохранённые материалы ещё не проверены на актуальность. В основном собраны первые страницы тем; это не полный свод действующих правил и не таблица наказаний.</p><p id="counts"></p><p><a href="index.json">Каталог JSON</a></p><ul id="servers"></ul><script>fetch('index.json').then(r=>r.json()).then(d=>{document.getElementById('counts').textContent=`Серверов: ${d.counts.servers} · документов: ${d.counts.documents} · привязок IP: ${d.counts.bindings}`;for(const s of d.servers){const li=document.createElement('li'),a=document.createElement('a');a.href=s.rulesUrl;a.textContent=s.name+' — '+s.documentCount+' документов';li.append(a);document.getElementById('servers').append(li)}})</script></html>''',encoding='utf-8')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='fetch_state'").fetchone():
        checks=[dict(x) for x in c.execute('SELECT * FROM fetch_state')]
        write(pub/'checks.json',{'warning':WARNING,'checks':checks})
    c.close();print(json.dumps(counts,ensure_ascii=False))

def bind(sid,key):
    if not re.fullmatch(r'[a-zA-Z0-9.-]+:[0-9]{1,5}',key) or not 0<int(key.rsplit(':',1)[1])<=65535:raise SystemExit('Expected verified hostname-or-IPv4:port')
    c=connect()
    if not c.execute('SELECT 1 FROM servers WHERE id=?',(sid,)).fetchone():raise SystemExit('Unknown server ID')
    collision=c.execute('SELECT server_key FROM bindings').fetchall()
    if any(filename(x[0])==filename(key) and x[0]!=key for x in collision):raise SystemExit('Filename collision')
    c.execute('INSERT OR REPLACE INTO bindings VALUES(?,?,?)',(key,sid,datetime.datetime.now(datetime.timezone.utc).isoformat()));c.commit();c.close();export()

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    imp=sub.add_parser('import');imp.add_argument('source',type=Path)
    sub.add_parser('export')
    b=sub.add_parser('bind');b.add_argument('--server',required=True);b.add_argument('--address',required=True)
    a=p.parse_args()
    if a.cmd=='import':init(a.source)
    elif a.cmd=='export':export()
    else:bind(a.server,a.address)
