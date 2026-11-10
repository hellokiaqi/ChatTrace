import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from .core import ArchiveError, atomic, tz

TASK_NAME = 'ChatTrace-Archive'


def task_xml(config, executable=None):
    time = config.get('schedule_time', '04:00')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time):
        raise ArchiveError('INVALID_SCHEDULE_TIME')
    executable = executable or sys.executable
    main = Path(__file__).resolve().parent.parent / 'main.py'
    arguments = subprocess.list2cmdline(['-X', 'utf8', str(main), '--config', config['config_path'], 'update'])
    now = datetime.now(tz('Asia/Shanghai'))
    hour, minute = map(int,time.split(':'))
    start = now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>ChatTrace private archive</Description></RegistrationInfo>
<Triggers><CalendarTrigger><StartBoundary>{start.isoformat()}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT3H</ExecutionTimeLimit><RestartOnFailure><Interval>PT15M</Interval><Count>2</Count></RestartOnFailure></Settings>
<Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(main.parent))}</WorkingDirectory></Exec></Actions></Task>'''


def schedule(config, action):
    name = TASK_NAME+'-QQ' if config['reader'] in ('qq-export','qq-db') else TASK_NAME
    if action == 'export':
        output = Path(config['config_path']).parent / 'private' / (name+'.xml')
        atomic(output, task_xml(config).encode('utf-16'))
        return str(output)
    if action == 'install':
        if config.get('live_acceptance_confirmed') is not True:
            raise ArchiveError('LIVE_ACCEPTANCE_REQUIRED')
        output = schedule(config, 'export')
        args = ['/Create', '/TN', name, '/XML', output, '/F']
    else:
        args = {'pause': ['/Change', '/TN', name, '/DISABLE'],
                'resume': ['/Change', '/TN', name, '/ENABLE'],
                'remove': ['/Delete', '/TN', name, '/F'],
                'run': ['/Run', '/TN', name],
                'status': ['/Query', '/TN', name, '/V', '/FO', 'LIST']}[action]
    result = subprocess.run(['schtasks.exe', *args], capture_output=True)
    if result.returncode:
        raise ArchiveError('SCHEDULER_COMMAND_FAILED_' + str(result.returncode))
    return result.stdout.decode(errors='replace')
