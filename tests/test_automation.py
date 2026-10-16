import unittest,sys,json,tempfile,sqlite3,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'automation'))
import manage,pipeline
class AutomationTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  source=manage.connect();source.backup(self.c);source.close();pipeline.migrate(self.c)
 def tearDown(self):self.c.close()
 def html(self,post='Текст правила',nav='',identity='post-1'):
  return f'<article class="message--post" data-content="{identity}"><div class="message-body"><div class="bbWrapper">{post}<script>bad()</script></div></div></article><div class="pageNav">{nav}</div>'.encode()
 def test_parse_and_scripts_not_executed(self):
  rows,links,pages=pipeline.parse_page(self.html(), 'https://forum.arizona-rp.com/threads/1/')
  self.assertEqual(rows[0]['text'],'Текст правила');self.assertNotIn('bad',rows[0]['text'])
 def test_pagination_all_pages(self):
  u='https://forum.arizona-rp.com/threads/1/'
  data={u:self.html(nav='<a href="/threads/1/page-2">2</a>'),u+'page-2':self.html('Второй пункт',identity='post-2')}
  rows,_,pages=pipeline.crawl_document(u,data.__getitem__)
  self.assertEqual(rows,['Текст правила','Второй пункт']);self.assertEqual(pages,2)
 def test_cap_rejects_partial_snapshot(self):
  u='https://forum.arizona-rp.com/threads/1/'
  with self.assertRaisesRegex(ValueError,'cap'):pipeline.crawl_document(u,lambda _:self.html(nav='<a href="/threads/1/page-2">2</a>'),1)
 def test_challenge_is_not_a_rule(self):
  with self.assertRaises(ValueError):pipeline.parse_page(b'<h1>Check your browser</h1>','https://forum.arizona-rp.com/threads/1/')
 def test_failure_leaves_database(self):
  old=self.c.execute('SELECT text FROM posts WHERE document_id=1 ORDER BY position').fetchall()
  with self.assertRaises(ValueError):pipeline.apply_document(self.c,1,[],[])
  self.assertEqual([r[0] for r in old],[r[0] for r in self.c.execute('SELECT text FROM posts WHERE document_id=1 ORDER BY position')])
 def test_revision_is_retained_and_unreviewed(self):
  old=[r[0] for r in self.c.execute('SELECT text FROM posts WHERE document_id=1 ORDER BY position')]
  self.assertTrue(pipeline.apply_document(self.c,1,['Новое правило'],[]))
  r=self.c.execute('SELECT old_posts_zlib FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
  self.assertEqual(json.loads(zlib.decompress(r[0])),old)
  self.assertEqual(self.c.execute('SELECT review_status FROM documents WHERE id=1').fetchone()[0],'unreviewed')
  self.assertFalse(pipeline.apply_document(self.c,1,['Новое правило'],[]))
 def test_fetch_success_without_content_change(self):
  rows=[r[0] for r in self.c.execute('SELECT text FROM posts WHERE document_id=1 ORDER BY position')]
  pipeline.apply_document(self.c,1,rows,[])
  self.assertIsNotNone(self.c.execute('SELECT last_success FROM fetch_state WHERE document_id=1').fetchone()[0])
 def test_source_url_allowlist(self):
  for u in ['http://forum.arizona-rp.com/threads/1/','https://evil.invalid/','https://forum.arizona-rp.com.evil.invalid/','https://name:pw@forum.arizona-rp.com/']:
   with self.assertRaises(ValueError):pipeline.valid_url(u)
 def data(self):return json.loads((ROOT/'tests/launcher-fixture.json').read_text())
 def test_mapping_requires_number_and_name(self):
  data=self.data();data['arizona'][0]['name']='Different'
  with self.assertRaises(ValueError):pipeline.proposed_bindings(self.c,data,lambda _:[])
 def test_repeated_additional_alias_never_trusted(self):
  data=self.data();result,conflicts,_=pipeline.proposed_bindings(self.c,data,lambda _:[])
  self.assertEqual(result['drake.arizona-rp.com:7777'],'drake')
  self.assertEqual(result['space.arizona-rp.com:7777'],'space')
  self.assertEqual(len(result),33)
 def test_shared_dns_address_excluded(self):
  result,conflicts,_=pipeline.proposed_bindings(self.c,self.data(),lambda _:['8.8.8.8'])
  self.assertNotIn('8.8.8.8:7777',result);self.assertIn('8.8.8.8:7777',conflicts)
 def test_failed_launcher_keeps_bindings(self):
  self.c.execute("INSERT OR REPLACE INTO bindings VALUES('example.invalid:7777','phoenix','test')");self.c.commit()
  with self.assertRaises(ValueError):pipeline.update_bindings(self.c,lambda _:b'{}',lambda _:[])
  self.assertEqual(self.c.execute("SELECT server_id FROM bindings WHERE server_key='example.invalid:7777'").fetchone()[0],'phoenix')
 def test_versions_deterministic_and_content_sensitive(self):
  a=manage.compatible(self.c,'phoenix','example.invalid:7777');b=manage.compatible(self.c,'phoenix','example.invalid:7777');self.assertEqual(a['version'],b['version'])
  self.c.execute("UPDATE posts SET text=text||' Changed' WHERE document_id=1")
  self.assertNotEqual(manage.compatible(self.c,'phoenix','example.invalid:7777')['version'],a['version'])
if __name__=='__main__':unittest.main()
