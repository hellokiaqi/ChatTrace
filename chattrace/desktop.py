"""Windows desktop controls for the existing archive workflow."""
import json
import os
import sqlite3
import subprocess
import sys
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox

from .core import ArchiveError, exclusive, load_config

PROJECT = Path(__file__).resolve().parents[1]
PAPER, WHITE, INK, MUTED, GREEN = '#f3f4ef', '#ffffff', '#202e28', '#52665d', '#216048'
CONFIGS = {'微信': 'config.json', 'QQ': 'config.qq.local.json'}
STAGES = {'collection': '读取新增消息', 'build': '整理检索文档', 'upload': '上传并核验文件'}
RESULTS = {'synced': '同步完成', 'synced_partial': '已同步，部分资料待补', 'failed': '同步失败'}


def snapshot(folder):
    logs = folder/'update-runs'
    latest = max(logs.glob('*.json'), default=None)
    run = json.loads(latest.read_bytes()) if latest else {}
    count = pending = None
    path = folder/'state.sqlite'
    if path.is_file():
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.2)
        try:
            count = db.execute('SELECT count(*) FROM messages').fetchone()[0]
            row = db.execute('SELECT media_pending FROM runs WHERE ended IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
            pending = row[0] if row else None
        finally:
            db.close()
    return run, count, pending


class Desktop:
    def __init__(self, root):
        self.root = root
        root.title('ChatTrace · 聊天归档')
        root.geometry('820x525')
        root.minsize(720, 490)
        root.configure(bg=PAPER)
        self.jobs = {}
        self.errors = {}
        self.buttons = {}
        self.platform = 'QQ' if (PROJECT/CONFIGS['QQ']).exists() else '微信'
        top = tk.Frame(root, bg=INK, padx=26, pady=15)
        top.pack(fill='x')
        self.label(top, 'ChatTrace', 16, 'white', bold=True).pack(side='left')
        for name in CONFIGS:
            button = self.button(top, name, lambda n=name: self.select(n))
            button.pack(side='right', padx=3)
            self.buttons[name] = button
        body = tk.Frame(root, bg=PAPER, padx=32, pady=19)
        body.pack(fill='both', expand=True)
        self.heading = self.label(body, '', 23, INK, bold=True)
        self.heading.pack(anchor='w')
        self.summary = self.label(body, '', 11, MUTED)
        self.summary.pack(anchor='w', pady=(2,15))
        card = tk.Frame(body, bg=WHITE, padx=20, pady=16)
        card.pack(fill='x')
        self.metrics = []
        for caption in ('已归档消息', '待补媒体消息'):
            group = tk.Frame(card, bg=WHITE, padx=8)
            group.pack(side='left', padx=(0,46))
            value = self.label(group, '—', 23, INK, bold=True)
            value.pack(anchor='w')
            self.label(group, caption, 10, MUTED).pack(anchor='w')
            self.metrics.append(value)
        self.sync = self.button(card, '立即同步', self.start, primary=True)
        self.sync.pack(side='right')
        self.state = self.label(body, '', 12, INK, bold=True)
        self.state.pack(anchor='w', pady=(16,0))
        self.detail = self.label(body, '', 10, MUTED)
        self.detail.pack(anchor='w', pady=(2,20))
        links = tk.Frame(body, bg=PAPER)
        links.pack(fill='x')
        self.links = {}
        for title, filename in [('打开聊天记录','index.html'), ('聊天统计','statistics.html')]:
            button = self.button(links, title, lambda f=filename: self.open_page(f))
            button.pack(side='left', padx=(0,10))
            self.links[filename] = button
        self.select(self.platform)
        self.tick()

    def label(self, parent, text, size, color, bold=False):
        return tk.Label(parent, text=text, fg=color, bg=parent['bg'], anchor='w',
                        font=('Microsoft YaHei UI', size, 'bold' if bold else 'normal'))

    def button(self, parent, title, command, primary=False):
        return tk.Button(parent, text=title, command=command, bg=GREEN if primary else WHITE,
                         fg='white' if primary else INK, activebackground='#d6e6db',
                         font=('Microsoft YaHei UI',11), relief='flat', cursor='hand2',
                         padx=16, pady=6, borderwidth=0)

    def select(self, name):
        self.platform = name
        for item, button in self.buttons.items():
            button.configure(bg='#d6e6db' if item==name else INK, fg=INK if item==name else 'white')
        self.refresh()

    def refresh(self):
        name = self.platform
        self.folder = None
        self.config = None
        run = {}
        count = pending = None
        detail = ''
        try:
            self.config = load_config(PROJECT/CONFIGS[name])
            self.folder = Path(self.config['archive_root'])
            run, count, pending = snapshot(self.folder)
        except (ValueError, OSError, ArchiveError, sqlite3.Error) as exc:
            detail = '未找到配置，请先完成首次配置。' if isinstance(exc,FileNotFoundError) else '暂时无法读取配置或归档状态。'
        self.heading.configure(text=(self.config['contact_name']+'的聊天归档') if self.config else name+'归档')
        self.summary.configure(text=name+'  /  本地归档')
        for widget, value in zip(self.metrics,(count,pending)):
            widget.configure(text='—' if value is None else f'{value:,}')
        job = self.jobs.get(name)
        active = job is not None and job.poll() is None
        if job and not active:
            if job.returncode not in (0,3):
                self.errors[name] = '本次同步未完成，请检查客户端登录和网络后重试。'
            del self.jobs[name]
        status = run.get('status')
        running = status=='running'
        if running and not active:
            try:
                with exclusive(self.folder):
                    running = False
            except ArchiveError:
                pass
        caption = RESULTS.get(status,'尚无同步记录')
        if active or running:
            caption = STAGES.get(run.get('stage'),'正在启动同步') if running else '正在启动同步'
        elif status=='running':
            caption = '上次同步已中断，可重新同步'
        timestamp = run.get('ended') or run.get('started')
        if not detail and timestamp:
            beijing = datetime.fromisoformat(timestamp).astimezone(timezone(timedelta(hours=8)))
            detail = beijing.strftime('%Y-%m-%d  %H:%M  北京时间')
        if status=='failed':
            detail += '  ·  '+str(run.get('error',''))
        self.state.configure(text=self.errors.get(name) or caption)
        self.detail.configure(text=detail)
        self.sync.configure(state='normal' if self.config and not (active or running) else 'disabled',
                            text='同步中…' if active or running else '立即同步')
        for filename,button in self.links.items():
            available = self.folder and (self.folder/'v4/human'/filename).is_file()
            button.configure(state='normal' if available else 'disabled')

    def start(self):
        name = self.platform
        try:
            self.jobs[name] = subprocess.Popen(
                [str(Path(sys.executable).with_name('python.exe')), '-X','utf8','main.py',
                 '--config',CONFIGS[name],'update'], cwd=PROJECT, creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.errors.pop(name,None)
        except OSError:
            messagebox.showerror('无法启动','请检查项目虚拟环境。',parent=self.root)
        self.refresh()

    def open_page(self, filename):
        try:
            os.startfile(self.folder/'v4/human'/filename)
        except OSError:
            messagebox.showerror('无法打开','请确认页面已生成并已设置默认浏览器。',parent=self.root)

    def tick(self):
        self.refresh()
        self.root.after(2000,self.tick)


if __name__=='__main__':
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
