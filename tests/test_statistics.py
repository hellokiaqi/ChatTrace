import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from chattrace.statistics import statistics_page


class StatisticsTests(unittest.TestCase):
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
"""
        subprocess.run(['node','-'],input=script+assertions,text=True,check=True)
