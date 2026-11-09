import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from chattrace.statistics import statistics_page


class StatisticsTests(unittest.TestCase):
    def test_topic_continuations_new_blocks_and_stale_manifest(self):
        from datetime import timezone
        from chattrace.core import digest, encode
        from chattrace.statistics import topic_activity
        messages=[dict(id=str(i),sender='self' if i%2==0 else 'friend',timestamp=f'2026-11-01T10:00:0{i}+00:00') for i in range(5)]
        def part(block,topic,mid):
            return dict(block_id=block,segment_id=topic,ids=[str(mid)])
        manifest=dict(llm_ready=True,input_hash=digest(encode(messages).encode()),documents=[
            dict(sessions=[part('a','one',0)]),
            dict(sessions=[part('a','one',1),part('a','two',2),part('b','three',3),part('b','four',4)])])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'manifest.json'
            self.assertIsNone(topic_activity(path,messages,timezone.utc))
            path.write_text(json.dumps(manifest),'utf8')
            self.assertEqual(topic_activity(path,messages,timezone.utc)['changes'],['2026-11-01T10:00:02','2026-11-01T10:00:04'])
            self.assertEqual(len(topic_activity(path,messages,timezone.utc)['topics']),3)
            manifest['documents']=[dict(sessions=[part('a','first',0),dict(block_id='a',segment_id='next',ids=['1','2'])]),
                                   dict(sessions=[dict(block_id='a',segment_id='next',ids=['3','4'])])]
            path.write_text(json.dumps(manifest),'utf8')
            self.assertEqual(topic_activity(path,messages,timezone.utc)['topics'],
                             [dict(time='2026-11-01T10:00:01',role='friend',otherTurns=2,switch=True)])
            self.assertIsNone(topic_activity(path,messages[:-1],timezone.utc))

    def test_private_event_projection_and_beijing_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quote = dict(sender='self',type='quote',timestamp='2026-11-01T16:00:00+00:00',
                         text='A 文\n🙂。',quoted_text='private quoted words')
            page = statistics_page(root,[quote,dict(quote,sender='friend',type='image'),
                                         dict(quote,timestamp=None)])
            source = page.read_text('utf8')
            data = json.loads(re.search(r'const data=(.+);',source)[1])
            self.assertEqual(data['undated'],1)
            self.assertCountEqual(data['events'],[['2026-11-02T00:00:00','self',4],
                                                  ['2026-11-02T00:00:00','friend',0]])
            self.assertEqual(set(data),{'events','undated','topicTimes','topicActivity'})
            self.assertNotIn('private quoted words',source)

    @unittest.skipUnless(shutil.which('node'),'Node is optional for testing the offline HTML logic')
    def test_browser_periods_and_individual_activity(self):
        template=Path(__file__).resolve().parents[1]/'chattrace/statistics.html'
        script=template.read_text('utf8').split('<script id="logic">')[1].split('</script>')[0]
        assertions="""
const assert=require('node:assert/strict');
assert.deepEqual(bounds('week','2027-01-03'),['2026-12-28T00:00:00','2027-01-04T00:00:00']);
assert.deepEqual(bounds('month','2028-02-07'),['2028-02-01T00:00:00','2028-03-01T00:00:00']);
const events=[
 ['2026-11-01T23:59:59','friend',1],
 ['2026-11-02T00:00:00','self',2],['2026-11-02T00:00:48','self',0],
 ['2026-11-02T00:01:01','system',0],['2026-11-02T18:00:00','friend',3],
 ['2026-11-03T00:00:00','unknown',0]];
const day=summarize(events,...bounds('day','2026-11-02'));
assert.equal(day.total,4);assert.equal(day.system,1);assert.equal(day.unknown,0);
assert.equal(day.self.messages,2);assert.equal(day.self.minutes.size,1);
assert.equal(day.self.chars,2);assert.equal(day.friend.hours[18],1);
const slice=summarize(events,...bounds('custom','','2026-11-02T00:00:48','2026-11-02T18:00:00'));
assert.equal(slice.total,2);assert.equal(slice.friend.messages,0);assert.equal(slice.self.chars,0);
assert.equal(summarize(events,...bounds('month','2026-12-01')).total,0);
assert.throws(()=>bounds('custom','','2026-11-03','2026-11-02'));
assert.throws(()=>bounds('custom','','2026-11-02T15:05','2026-11-02T15:05:00'));
const turns=[
 ['2026-11-01T23:58:00','self',1],['2026-11-01T23:59:59','self',1],
 ['2026-11-02T00:00:00','system',0],['2026-11-02T00:00:03','friend',1],
 ['2026-11-02T00:00:03','self',1],['2026-11-02T01:30:03','friend',1],
 ['2026-11-02T03:00:04','self',1]];
const t=summarize(turns,...bounds('day','2026-11-02'),['2026-11-01T23:59:59','2026-11-02T00:00:03']);
assert.equal(t.self.turns,2);assert.equal(t.friend.turns,2);assert.equal(t.rounds,2);
assert.equal(t.longest,2);
assert.equal(t.self.replies,1);assert.equal(t.self.replySeconds,0);assert.equal(t.friend.replies,2);
assert.equal(t.friend.replySeconds,5404);assert.equal(t.topics,1);
assert.equal(quantile([], .5),null);assert.equal(quantile([8],.9),8);assert.equal(quantile([0,10],.25),2.5);
const sample=[
 ['2026-01-01T09:00:00','self',1],['2026-01-01T23:59:00','friend',1],
 ['2026-01-02T00:01:00','self',1],['2026-01-02T00:01:30','friend',1],['2026-01-02T00:02:00','self',1],
 ['2026-01-02T03:00:00','self',1],['2026-01-02T06:00:00','friend',1]];
const jan1=summarize(sample,...bounds('day','2026-01-01'));
const jan2=summarize(sample,...bounds('day','2026-01-02'));
assert.equal(jan2.self.paused.length,0);assert.deepEqual(jan2.friend.short,[30]);
"""
        subprocess.run(['node','-'],input=script+assertions,text=True,check=True)
