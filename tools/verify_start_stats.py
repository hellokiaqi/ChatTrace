"""Reconcile displayed attribution with SQLite window functions. Requires Node for the page logic."""
import json
import re
import sqlite3
import subprocess
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chattrace.core import load_config

root = Path(load_config('config.json')['archive_root'])
page = (root/'v4/human/statistics.html').read_text('utf8')
data = json.loads(re.search(r'const data=(.+);',page)[1])
db = sqlite3.connect((root/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
db.row_factory = sqlite3.Row
db.execute('CREATE TEMP TABLE decisions(id PRIMARY KEY,model,review)')
db.executemany('insert into decisions values (?,?,?)',[(b['id'],(b['suggestion'] or {}).get('label'),b['review']) for b in data['boundaries']])
sql = """
WITH messages_ AS (
 SELECT printf('M%06d',id) id, json_extract(payload,'$.sender') role,
        julianday(json_extract(payload,'$.timestamp')) t,json_extract(payload,'$.sequence') seq FROM messages
), people AS (
 SELECT *,round((t-lag(t) over w)*86400,3) gap,lag(role) over w prev FROM messages_
 WHERE role IN ('self','friend') AND t IS NOT NULL WINDOW w AS (ORDER BY t,seq,id)
), events AS (
 SELECT p.*,CASE WHEN gap>:seconds THEN CASE :basis WHEN 'old' THEN 'new'
 WHEN 'review' THEN coalesce(review,'uncertain') ELSE coalesce(review,model,'uncertain') END END label
 FROM people p LEFT JOIN decisions d USING(id)
), grouped AS (
 SELECT *,sum(CASE WHEN gap IS NULL OR label IN ('new','uncertain') THEN 1 ELSE 0 END)
 OVER(ORDER BY t,seq,id) grp FROM events
), segments AS (
 SELECT grp,max(CASE WHEN label IN ('new','uncertain') THEN label END) origin,
 max(CASE WHEN label IN ('new','uncertain') THEN role END) starter,
 sum(CASE WHEN gap IS NULL OR label IN ('new','uncertain') OR role!=prev THEN 1 ELSE 0 END)/2 rounds
 FROM grouped GROUP BY grp
), closed AS (SELECT *,lead(origin) OVER(ORDER BY grp) following FROM segments)
SELECT role,sum(label='new') starts,sum(label='delayed') delayed,sum(label='uncertain') uncertain,
 (SELECT count(*) FROM closed WHERE origin='new' AND following='new' AND starter=role) samples,
 (SELECT coalesce(sum(rounds),0) FROM closed WHERE origin='new' AND following='new' AND starter=role) rounds
 FROM events GROUP BY role ORDER BY role
"""
logic=page.split('<script id="logic">')[1].split('</script>')[0]
js=logic+"""
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>input+=s);process.stdin.on('end',()=>{
 const d=JSON.parse(input),out={};for(const b of ['review','model','old'])for(const h of [1.5,3,6,12,24]){
 const r=attribution(d.events,d.boundaries,'','',h,b);out[b+h]=['friend','self'].map(k=>{
 const p=r[k];return [k,p.new,p.delayed,p.uncertain,p.scenes.length,p.scenes.reduce((n,s)=>n+s.rounds,0)]});}
 console.log(JSON.stringify(out));});
"""
browser=json.loads(subprocess.run(['node','-e',js],input=json.dumps(data).encode(),capture_output=True,check=True).stdout)
for basis in ('review','model','old'):
    for hours in (1.5,3,6,12,24):
        result=[[v if v is not None else 0 for v in row] for row in db.execute(sql,dict(seconds=hours*3600,basis=basis))]
        assert result==browser[basis+str(hours)],(basis,hours,result,browser[basis+str(hours)])
        print(basis,hours,result)
db.close()
