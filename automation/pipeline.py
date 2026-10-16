#!/usr/bin/env python3
"""Bounded read-only forum collector. No browser login, secrets, or rule execution."""
import argparse,datetime,gzip,hashlib,ipaddress,json,re,socket,sqlite3,sys,time,urllib.request,urllib.error,urllib.parse,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));import manage
API='https://api-samp.arizona-five.com/launcher/servers-data'
ALLOWED={'forum.arizona-rp.com','api-samp.arizona-five.com'}

def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def valid_url(url):
 p=urllib.parse.urlsplit(url)
 if p.scheme!='https' or p.hostname not in ALLOWED or p.username or p.password or p.port not in [None,443]:raise ValueError('URL outside source allowlist')
 return url
class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  valid_url(newurl);return super().redirect_request(req,fp,code,msg,headers,newurl)
class Fetcher:
 def __init__(self,delay=1.2):self.delay=delay;self.last=0;self.opener=urllib.request.build_opener(Redirect())
 def __call__(self,url):
  valid_url(url)
  if time.monotonic()>getattr(self,'deadline',float('inf')):raise TimeoutError('Collector time budget reached')
  for attempt in range(2):
   time.sleep(max(0,self.delay-(time.monotonic()-self.last)));self.last=time.monotonic()
   try:
    req=urllib.request.Request(url,headers={'User-Agent':'ArizonaHelperRules/1.0 (public reference collector; no authentication)','Accept':'text/html,application/json'})
    with self.opener.open(req,timeout=25) as r:
     valid_url(r.url);raw=r.read(5*1024*1024+1)
     if len(raw)>5*1024*1024:raise ValueError('Source response exceeds 5MB')
     return raw
   except urllib.error.HTTPError as e:
    if e.code in [429,500,502,503,504] and attempt==0:time.sleep(10);continue
    raise

class BrowserFetcher(Fetcher):
 """Normal public-page Chromium rendering for JS-only pages, not stealth/login."""
 def __init__(self,delay=1.2):
  super().__init__(delay)
  from playwright.sync_api import sync_playwright
  self.pw=sync_playwright().start()
  self.browser=self.pw.chromium.launch(headless=True)
  self.context=self.browser.new_context(locale='ru-RU')
  self.page=self.context.new_page()
 def __call__(self,url):
  valid_url(url)
  if urllib.parse.urlsplit(url).hostname!='forum.arizona-rp.com':return super().__call__(url)
  time.sleep(max(0,self.delay-(time.monotonic()-self.last)));self.last=time.monotonic()
  if time.monotonic()>getattr(self,'deadline',float('inf')):raise TimeoutError('Collector time budget reached')
  self.page.goto(url,wait_until='domcontentloaded',timeout=30000);valid_url(self.page.url)
  # Persistent browser checks/CAPTCHAs/sign-in stop the read; never forge success.
  self.page.wait_for_selector('article.message--post, .structItem--thread',timeout=30000)
  raw=self.page.content().encode('utf-8')
  if len(raw)>5*1024*1024:raise ValueError('Rendered source exceeds 5MB')
  return raw
 def close(self):
  self.context.close();self.browser.close();self.pw.stop()

def migrate(c):
 c.executescript('''
 CREATE TABLE IF NOT EXISTS fetch_state(document_id INTEGER PRIMARY KEY REFERENCES documents(id),last_attempt TEXT,last_success TEXT,last_error TEXT);
 CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL REFERENCES documents(id),saved_at TEXT NOT NULL,old_posts_zlib BLOB NOT NULL,new_sha256 TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS auto_bindings(server_key TEXT PRIMARY KEY,source_url TEXT NOT NULL,checked_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT NOT NULL,event TEXT NOT NULL,details TEXT NOT NULL);
 ''');c.commit()

def parse_page(raw,url):
 from bs4 import BeautifulSoup
 soup=BeautifulSoup(raw,'html.parser');p=urllib.parse.urlsplit(url);match=re.match(r'^/threads/(\d+)',p.path)
 if not match:raise ValueError('Not a numeric thread URL')
 thread=match[1];articles=soup.select('article.message--post')
 if not articles:raise ValueError('No forum messages: login/challenge/deleted page; previous text retained')
 posts=[];links=[]
 for art in articles:
  body=art.select_one('.message-body .bbWrapper')
  if body is None:raise ValueError('Unexpected message layout; no partial replacement')
  for node in body.select('script,style,.bbCodeBlock-expandLink'):node.decompose()
  text=manage.clean(body.get_text('\n',strip=True))
  if not text:raise ValueError('Empty message body')
  post_id=art.get('data-content') or art.get('id')
  if not post_id:raise ValueError('Missing post identity')
  posts.append({'id':post_id,'text':text})
  for a in body.select('a[href]'):
   u=urllib.parse.urljoin(url,a['href'])
   if urllib.parse.urlsplit(u).scheme in ['http','https']:links.append({'title':a.get_text(' ',strip=True),'url':u})
 pages=set()
 for a in soup.select('.pageNav a[href]'):
  u=urllib.parse.urljoin(url,a['href']);q=urllib.parse.urlsplit(u)
  if q.hostname=='forum.arizona-rp.com' and re.fullmatch(r'/threads/'+thread+r'/(?:page-[1-9]\d*)?',q.path):pages.add(urllib.parse.urlunsplit(('https',q.netloc,q.path,'','')))
 return posts,links,pages

def crawl_document(url,fetch,max_pages=40):
 start='https://forum.arizona-rp.com/threads/'+re.search(r'/threads/(\d+)',url)[1]+'/'
 todo=[start];seen=set();posts={};links=[]
 while todo:
  page=todo.pop(0)
  if page in seen:continue
  if len(seen)>=max_pages:raise ValueError('Pagination cap reached: previous snapshot retained')
  seen.add(page);rows,newlinks,pages=parse_page(fetch(page),page)
  for p in rows:
   if p['id'] in posts and posts[p['id']]!=p['text']:raise ValueError('Post changed during pagination; retry next cycle')
   posts[p['id']]=p['text']
  links.extend(newlinks)
  todo.extend(sorted(pages-seen-set(todo),key=lambda u:int(re.search(r'page-(\d+)',u)[1]) if 'page-' in u else 1))
 if not posts:raise ValueError('Empty topic')
 # Numeric post IDs establish page-independent stable ordering.
 ordered=sorted(posts.items(),key=lambda p:int(re.search(r'\d+',p[0])[0]))
 return [text for _,text in ordered],list({(x['title'],x['url']):x for x in links}.values()),len(seen)

def apply_document(c,did,rows,links):
 if not rows:raise ValueError('Refuse empty update')
 old=[r[0] for r in c.execute('SELECT text FROM posts WHERE document_id=? ORDER BY position',(did,))]
 changed=old!=rows;at=stamp()
 with c:
  if changed:
   raw=json.dumps(rows,ensure_ascii=False).encode();sha=hashlib.sha256(raw).hexdigest()
   c.execute('INSERT INTO revisions(document_id,saved_at,old_posts_zlib,new_sha256) VALUES(?,?,?,?)',(did,at,zlib.compress(json.dumps(old,ensure_ascii=False).encode(),9),sha))
   c.execute('DELETE FROM posts WHERE document_id=?',(did,))
   c.executemany('INSERT INTO posts VALUES(?,?,?,?)',[(did,i,text,hashlib.sha256(text.encode()).hexdigest()) for i,text in enumerate(rows,1)])
   c.execute("UPDATE documents SET captured_at=?,has_more_pages=0,review_status='unreviewed',links_json=? WHERE id=?",(at,json.dumps(links,ensure_ascii=False),did))
  c.execute('UPDATE documents SET has_more_pages=0 WHERE id=?',(did,))
  c.execute('INSERT OR REPLACE INTO fetch_state VALUES(?,?,?,NULL)',(did,at,at))
 return changed

def norm(x):return re.sub('[^a-z0-9]','',x.lower())
def proposed_bindings(c,data,resolver):
 rows=data.get('arizona')
 if not isinstance(rows,list) or len(rows)<25:raise ValueError('Launcher schema/list incomplete')
 servers={r['server_number']:r for r in c.execute("SELECT * FROM servers WHERE platform='pc'")}
 candidates={};matched=set();dns_errors=[]
 for row in rows:
  s=servers.get(row.get('number'))
  if not s or norm(s['id'])!=norm(str(row.get('name',''))):continue
  if row['number'] in matched:raise ValueError('Duplicate server number')
  matched.add(row['number']);port=row.get('port')
  if type(port)!=int or not 1<=port<=65535:raise ValueError('Bad launcher port')
  # Primary endpoint and direct DNS answers only: additionalIps contains aliases
  # duplicated between Drake/Space/Home, so it must NOT be trusted automatically.
  host=str(row.get('ip','')).lower()
  try:ipaddress.IPv4Address(host)
  except ValueError:
   if not re.fullmatch(r'[a-z0-9-]+\.arizona-rp\.com',host):raise ValueError('Unexpected primary host')
  hosts={host}
  try:
   for address in resolver(host):
    ip=ipaddress.IPv4Address(address)
    if not ip.is_global:raise ValueError('Non-public DNS address')
    hosts.add(str(ip))
  except (OSError,socket.gaierror):dns_errors.append(host)
  for h in hosts:candidates.setdefault(f'{h}:{port}',set()).add(s['id'])
 if matched!=set(servers):raise ValueError('Launcher number/name mapping not complete; previous bindings retained')
 conflict={key:sorted(ids) for key,ids in candidates.items() if len(ids)>1}
 return {key:next(iter(ids)) for key,ids in candidates.items() if len(ids)==1},conflict,dns_errors

def update_bindings(c,fetch,resolver=None):
 data=json.loads(fetch(API));resolver=resolver or (lambda host:[x[4][0] for x in socket.getaddrinfo(host,None,socket.AF_INET,socket.SOCK_DGRAM)])
 bindings,conflicts,errors=proposed_bindings(c,data,resolver);at=stamp()
 with c:
  # Reconcile only auto-managed rows. Do not overwrite operator-owned mappings.
  for old in c.execute('SELECT server_key FROM auto_bindings').fetchall():c.execute('DELETE FROM bindings WHERE server_key=?',(old[0],))
  c.execute('DELETE FROM auto_bindings')
  for key,sid in bindings.items():
   existing=c.execute('SELECT server_id FROM bindings WHERE server_key=?',(key,)).fetchone()
   if existing:
    if existing[0]!=sid:conflicts[key]=['operator',sid]
    continue
   c.execute('INSERT INTO bindings VALUES(?,?,?)',(key,sid,at));c.execute('INSERT INTO auto_bindings VALUES(?,?,?)',(key,API,at))
  c.execute('INSERT INTO audit(at,event,details) VALUES(?,?,?)',(at,'launcher_bindings',json.dumps({'count':len(bindings),'conflicts':conflicts,'dnsErrors':errors})))
 return {'count':len(bindings),'conflicts':conflicts,'dnsErrors':errors}

KEYWORDS=re.compile(r'кодекс|устав|мануал|база знаний|правил|закон|конституц|регламент|инструкц|полномоч',re.I)
def discover(c,seeds,fetch,limit=6):
 from bs4 import BeautifulSoup
 cursor=int((c.execute("SELECT value FROM metadata WHERE key='section_cursor'").fetchone() or ['0'])[0]);added=0
 section_errors=0
 for offset in range(min(limit,len(seeds))):
  seed=seeds[(cursor+offset)%len(seeds)]
  try:
   url=seed['url'];valid_url(url);soup=BeautifulSoup(fetch(url),'html.parser')
   if not soup.select('.structItem--thread'):raise ValueError('Section layout/login/challenge')
   for a in soup.select('.structItem-title a[href]'):
    title=a.get_text(' ',strip=True);m=re.search(r'/threads/(\d+)',a['href'])
    if not m or not KEYWORDS.search(title):continue
    thread='https://forum.arizona-rp.com/threads/'+m[1]+'/'
    if c.execute('SELECT 1 FROM documents WHERE server_id=? AND url=?',(seed['serverId'],thread)).fetchone():continue
    c.execute('INSERT INTO documents(server_id,scope,title,url,has_more_pages,links_json) VALUES(?,?,?,?,1,?)',(seed['serverId'],seed['scope'],title,thread,'[]'));added+=1
  except Exception as e:
   c.execute('INSERT INTO audit(at,event,details) VALUES(?,?,?)',(stamp(),'discovery_error',str(e)[:500]));section_errors+=1
   if section_errors>=3:break
 c.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('section_cursor',str((cursor+min(limit,len(seeds)))%max(1,len(seeds)))))
 c.commit();return added

def run(args):
 c=manage.connect();migrate(c);fetch=BrowserFetcher() if args.browser else Fetcher();deadline=time.monotonic()+2400;report={'at':stamp(),'bindings':None,'updated':0,'unchanged':0,'errors':[],'pages':0}
 try:report['bindings']=update_bindings(c,fetch)
 except Exception as e:report['errors'].append({'stage':'launcher','error':str(e)[:500]})
 fetch.deadline=deadline
 seeds=json.loads((ROOT/'config/sections.json').read_text());report['newTopics']=discover(c,seeds,fetch,args.sections)
 docs=c.execute('''SELECT d.* FROM documents d LEFT JOIN fetch_state f ON f.document_id=d.id ORDER BY coalesce(f.last_attempt,''),d.id LIMIT ?''',(args.limit,)).fetchall()
 consecutive_errors=0
 for d in docs:
  if time.monotonic()>deadline:report['timeBudgetReached']=True;break
  try:
   posts,links,pages=crawl_document(d['url'],fetch,args.max_pages)
   changed=apply_document(c,d['id'],posts,links);consecutive_errors=0;report['updated' if changed else 'unchanged']+=1;report['pages']+=pages
  except Exception as e:
   err=str(e)[:500];at=stamp();c.execute('''INSERT INTO fetch_state(document_id,last_attempt,last_error) VALUES(?,?,?) ON CONFLICT(document_id) DO UPDATE SET last_attempt=excluded.last_attempt,last_error=excluded.last_error''',(d['id'],at,err));c.commit()
   report['errors'].append({'documentId':d['id'],'url':d['url'],'error':err});consecutive_errors+=1
   if consecutive_errors>=3:report['circuitBreaker']='Three consecutive document errors; retained previous snapshots';break
 c.close()
 if args.browser:fetch.close()
 manage.write(ROOT/'data/last-run.json',report);manage.export();manage.write(ROOT/'public/status.json',report)
 print(json.dumps({k:v for k,v in report.items() if k!='errors'},ensure_ascii=False));print('errors:',len(report['errors']))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--browser',action='store_true');p.add_argument('--limit',type=int,default=80);p.add_argument('--sections',type=int,default=6);p.add_argument('--max-pages',type=int,default=40);a=p.parse_args()
 if not 1<=a.limit<=1000 or not 0<=a.sections<=100 or not 1<=a.max_pages<=100:raise SystemExit('Invalid workload limits')
 run(a)
