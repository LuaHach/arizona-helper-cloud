from pathlib import Path
import json,sys,sqlite3,tempfile,shutil,subprocess
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import manage
c=manage.connect();assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert not c.execute('PRAGMA foreign_key_check').fetchall()
assert c.execute('SELECT count(*) FROM documents').fetchone()[0]>=712
assert c.execute('SELECT count(*) FROM servers').fetchone()[0]==36
assert c.execute('SELECT count(*) FROM bindings').fetchone()[0]>=0
assert not c.execute("SELECT id FROM documents WHERE review_status<>'unreviewed'").fetchall()
assert not c.execute("SELECT document_id FROM posts WHERE text LIKE '%\"lightbox_close\"%'").fetchall()
# Every server export preserves source text and separates scopes.
for row in c.execute('SELECT * FROM servers'):
 p=ROOT/'public/servers'/f"{row['id']}.json";data=json.loads(p.read_text())
 assert data['server']['id']==row['id']
 for d in data['documents']:
  assert d['server_id'] in [None,row['id']]
  assert d['url'].startswith('https://forum.arizona-rp.com/')
  assert d['review_status']=='unreviewed'
  for post in d['posts']:
   assert post['text']==c.execute('SELECT text FROM posts WHERE document_id=? AND position=?',(d['id'],post['position'])).fetchone()[0]
 # Reserved example address is for compatibility tests only, never exported.
 package=manage.compatible(c,row['id'],'example.invalid:7777')
 assert package['serverKey']=='example.invalid:7777'
 assert package['reviewStatus']=='unreviewed'
 for bucket in ['fines','wanted']:
  assert len(package[bucket])<=1000
  for e in package[bucket]:
   assert manage.WARNING in e['text'] and 'Источник:' in e['text']
   assert len(e['text'].encode())<=20000 and len(e['title'].encode())<=512
 assert len(json.dumps(package,ensure_ascii=False,indent=2).encode())<4*1024*1024
# UTF-8 splitting never drops/replaces characters.
s='Текст правила 🧾\n'*3000
assert ''.join(manage.chunks(s))==s
assert all(len(x.encode())<=15000 for x in manage.chunks(s))
assert manage.filename('127.0.0.1:7777')=='127.0.0.1_7777'
# Bind/export only in a temporary copy; source DB stays unbound.
with tempfile.TemporaryDirectory() as t:
 out=Path(t);shutil.copy(ROOT/'manage.py',out/'manage.py');shutil.copytree(ROOT/'data',out/'data')
 r=subprocess.run([sys.executable,str(out/'manage.py'),'bind','--server','phoenix','--address','example.invalid:7777'],capture_output=True,text=True)
 assert r.returncode==0,r.stderr
 p=out/'public/rules/example.invalid_7777.json';assert p.exists()
 data=json.loads(p.read_text());assert data['serverId']=='phoenix'
 # Validate with the real Helper storage validator, not a copied Python schema.
 from lupa.lua51 import LuaRuntime
 lua=LuaRuntime(unpack_returned_tuples=True)
 lua.execute("package.path = %r .. '/?.lua;' .. package.path" % (ROOT/'tests/helper-validator').as_posix())
 validate=lua.eval("require('aph.storage').validateRules")
 def table(x):
  if isinstance(x,dict):return lua.table_from({k:table(v) for k,v in x.items()})
  if isinstance(x,list):return lua.table_from([table(v) for v in x])
  return x
 assert validate(table(data),'example.invalid:7777') is True
 bad=validate(table(data),'other.invalid:7777');assert isinstance(bad,tuple) and bad[0] is None
c.close()
print('PASS: SQLite integrity/FKs, 36 JSON exports, 712 documents, UTF-8 chunks, size limits, isolated binding/export, actual Helper Lua validator and wrong-server rejection.')
print('NOT TESTED: live HTTPS hosting, current forum validity, GTA/MoonLoader network download.')
