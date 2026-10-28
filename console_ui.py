"""Nonblocking key handling for the interactive WSL watchdog console.

Accept uppercase M (Shift+M in normal keyboard layout) only when the polling
window owns a Unix TTY. Never steal keystrokes from an agent tmux session.
"""
from __future__ import annotations
import os,select,sys,termios,tty,subprocess,shutil,re
from pathlib import Path
from model_picker import saved
from reset_times import local_timezone, format_reset_local
from pause_control import paused
from engine import load_json
from alerts import pending_question, is_auto_stall
from background_wait import current as current_background_wait
from daybreak_watch import description as daybreak_description


class ConsoleUI:
    def __init__(self,root):
        self.root=Path(root)
        self.stream=None
        self.fd=None
        self.saved_mode=None
        self.available=False
        # Keep one editable status line at the bottom of LIVE output. Unlike
        # print(footer) on every poll, the old footer is erased in-place before
        # any new log lines are appended, so scrollback contains no duplicates.
        self.ansi_output=False
        self.footer_visible=False
        self.fallback_footer_printed=False

    def __enter__(self):
        try:
            if sys.stdin.isatty():
                self.stream=sys.stdin
            else:
                self.stream=open('/dev/tty','r',encoding='utf-8',errors='ignore')
            self.fd=self.stream.fileno()
            self.saved_mode=termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.available=True
        except (OSError,ValueError,termios.error):
            if self.stream is not sys.stdin and self.stream is not None:self.stream.close()
            self.stream=None
            self.available=False
        self.ansi_output=bool(sys.stdout.isatty() and os.environ.get('TERM','') not in ('dumb','unknown'))
        return self

    def __exit__(self,*args):
        self.clear_footer()
        self.restore()
        if self.stream is not sys.stdin and self.stream is not None:self.stream.close()

    def restore(self):
        if self.available and self.fd is not None and self.saved_mode is not None:
            try:termios.tcsetattr(self.fd,termios.TCSADRAIN,self.saved_mode)
            except (OSError,termios.error):pass
        self.available=False

    def enable(self):
        if self.fd is not None and self.saved_mode is not None:
            try:tty.setcbreak(self.fd);self.available=True
            except (OSError,termios.error):self.available=False

    def footer(self):
        chosen=saved()
        def describe(a):return f'{chosen[a]["model"]}/{chosen[a]["effort"]}'
        hotkey='Shift+M: change models/efforts' if self.available else 'change models: watchdog-windows.ps1 -Command models'
        return f'━━ {hotkey}  |  Claude {describe("claude")}  |  Codex {describe("codex")}  ━━'

    def clear_footer(self):
        """Remove the lone footer *on screen*, leaving no historical copies.

        The output cursor stays on the same row as the footer. This works in
        Windows Terminal via WSL ConPTY and doesn't require a curses screen
        (which would swallow regular terminal scrollback).
        """
        if self.footer_visible and self.ansi_output:
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
        self.footer_visible=False

    def write_messages(self,messages, *, timestamp=None):
        """Display the next log batch without depositing a copy of the footer."""
        self.clear_footer()
        if timestamp is None:
            import time
            timestamp=time.strftime('[%Y-%m-%d %H:%M:%S] ')
        for item in messages:
            print(timestamp+item,flush=True)
        self.show_footer()

    def show_footer(self):
        footer=self.footer()
        if self.ansi_output:
            # Keep the footer to one physical line; wrapping it would defeat
            # in-place erasure in narrow or resized terminals.
            columns=shutil.get_terminal_size(fallback=(100,24)).columns
            footer=footer[:max(1,columns-1)]
            sys.stdout.write('\r\x1b[2K'+footer)
            sys.stdout.flush()
            self.footer_visible=True
        elif not self.fallback_footer_printed:
            # Redirected output / terminals without VT sequences: once only.
            print(footer,flush=True)
            self.fallback_footer_printed=True

    def read_key(self,timeout=.1):
        if not self.available:return False
        try:
            ready,_,_=select.select([self.fd],[],[],timeout)
            if not ready:return False
            key=os.read(self.fd,64)
            return b'M' in key and len(key)==1  # Shift+M, not lowercase m
        except (OSError,ValueError):return False

    def choose_models(self):
        if not self.available:return False
        self.restore()
        print('\nOpening model/effort selector (only affects NEXT launched agent)...',flush=True)
        try:
            with open('/dev/tty','rb',buffering=0) as entry:
                cp=subprocess.run([sys.executable,str(self.root/'.agent-watchdog'/'model_picker.py'),'choose'],
                                  stdin=entry,cwd=self.root,check=False)
            return cp.returncode==0
        finally:
            self.enable()


class DashboardUI(ConsoleUI):
    """Compact terminal dashboard with durable, event-only scrollback.

    Rewrites the live rows in place. Only transitions and useful diagnostics are
    appended to the scrollback. Never changes the polling or handoff decisions.
    The ordinary ConsoleUI remains available for backwards compatibility.
    """

    _AGENT = re.compile(
        r'^(claude|codex)\s+limiting\s+(session|weekly|other):\s*'
        r'([\d.]+)% used \(([\d.]+)% left\)\s+—\s+(.+)$')
    _WINDOW = re.compile(
        r'^\s+(session|weekly|other)\s+([\d.]+)% used,\s*([\d.]+)% left\s+\(metric\s+[^;)]+(?:;\s*reset_at=([\d.]+))?\)$')
    _OFFLINE = re.compile(r'^(claude|codex)\s+TELEMETRY UNAVAILABLE\s+\((\d+)s\):\s*(.+)$')
    _COLORS = {'green':'\x1b[92m','yellow':'\x1b[93m','red':'\x1b[91m',
               'cyan':'\x1b[96m','purple':'\x1b[95m','dim':'\x1b[90m',
               'white':'\x1b[97m'}

    def __init__(self, root):
        super().__init__(root)
        self.live_lines = 0
        self.stats = {a:{'state':'checking','windows':{}} for a in ('claude','codex')}
        self.recent_events = {}
        self.last_plain = None
        self.last_plain_time = 0
        self.last_poll = ''
        try:
            import json
            config_path = self.root / '.agent-watchdog' / 'config.json'
            config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
            selected_timezone = config.get('display_timezone', 'auto')
        except (OSError, ValueError, TypeError):
            selected_timezone = 'auto'
        self.display_zone = local_timezone(selected_timezone)

    def clear_footer(self):
        """Rewind the whole dashboard so it never grows into log history."""
        if self.live_lines and self.ansi_output:
            # The cursor is at column zero immediately after the last row's LF.
            sys.stdout.write(f'\x1b[{self.live_lines}A\r\x1b[J')
            sys.stdout.flush()
        self.live_lines=0
        self.footer_visible=False

    @staticmethod
    def _severity(stage):
        lower=stage.lower()
        if 'offline' in lower or 'unavailable' in lower: return 'red'
        if 'complete' in lower: return 'cyan'
        if 'handoff' in lower: return 'red'
        if 'prepare' in lower: return 'yellow'
        return 'green'

    def _colour(self, value, name):
        if not self.ansi_output or os.environ.get('NO_COLOR') is not None or os.environ.get('CLICOLOR') == '0':
            return value
        return self._COLORS[name]+value+'\x1b[0m'

    @staticmethod
    def _bar(used, cells):
        used=max(0.,min(100.,float(used)))
        count=min(cells,max(0,int(round(used*cells/100.))))
        return '█'*count+'░'*(cells-count)

    def _extract(self, messages):
        events=[]
        owner=None
        for raw in messages:
            msg=re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', raw).strip()
            match=self._AGENT.match(raw)
            if match:
                agent,limiting,used,left,stage=match.groups()
                stage=stage.replace(' [THRESHOLD CHANGED]','')
                prior=self.stats[agent].get('state')
                self.stats[agent].update(state=stage, limiting=limiting, used=float(used), left=float(left), online=True)
                owner=agent
                if prior not in ('checking',stage):
                    events.append((f'{agent.upper()}: {stage}',self._severity(stage)))
                continue
            match=self._OFFLINE.match(raw)
            if match:
                agent,seconds,reason=match.groups()
                self.stats[agent].update(state='telemetry unavailable',online=False)
                owner=None
                if self.recent_events.get('last_offline_'+agent)!='offline':
                    events.append((f'{agent.upper()} telemetry unavailable ({seconds}s). Quota not assumed. {reason}', 'red'))
                self.recent_events['last_offline_'+agent]='offline'
                continue
            match=self._WINDOW.match(raw)
            if match and owner:
                window,used,left,reset_ts=match.groups()
                self.stats[owner]['windows'][window]={'used':float(used),'left':float(left),
                    'reset_ts':float(reset_ts) if reset_ts is not None else None}
                self.recent_events.pop('last_offline_'+owner,None)
                continue
            owner=None
            if msg:
                severity='red' if any(x in msg.upper() for x in ('NOT DELIVERED','FAILED','ERROR','UNAVAILABLE')) else (
                    'yellow' if any(x in msg.upper() for x in ('ALERT','RECOVERY','CHECKPOINT','QUEUED USER QUESTION')) else 'cyan')
                events.append((msg[:450],severity))
        return events

    def _pending(self):
        if paused(self.root):
            return ('PAUSED BY USER — automation disabled; use -Command resume to continue','yellow')
        worker=load_json(self.root/'.agent-watchdog'/'worker.json',{}) or {}
        if worker.get('startup_status')=='failed':
            return (f'{str(worker.get("agent","Agent")).capitalize()} startup failed — inspect the agent terminal', 'red')
        decision=pending_question(self.root)
        if decision:
            name=str(decision.get('agent','agent')).capitalize()
            if is_auto_stall(decision):
                return (f'{name} stalled — review session; -Command status for details','yellow')
            return (f'{name} needs approval — answer in the agent terminal (-Command status for question)', 'yellow')
        daybreak=daybreak_description(self.root)
        if daybreak:
            return (daybreak,'yellow')
        waiting=current_background_wait(self.root)
        if waiting:
            name=str(waiting.get('agent','agent')).capitalize()
            return (f'{name} waiting for background job — no user answer needed','cyan')
        queued=load_json(self.root/'.agent-watchdog'/'codex-questions.json',{}) or {}
        if queued.get('active') and queued.get('count',0)>0:
            n=queued['count']
            return (f'Codex has {n} queued question{"s" if n!=1 else ""} — answer via Shift+Left in Codex', 'yellow')
        c=self.stats['claude'];d=self.stats['codex']
        if not c.get('online',True) or not d.get('online',True):
            return ('Quota API issue - waiting for verified readings','red')
        waiting=[]
        for label,s in [('Claude',c),('Codex',d)]:
            if s.get('state','').startswith('handoff complete') and 'awaiting' in s['state']:
                waiting.append(label)
        if waiting:
            if all(self.stats[a].get('left',100)<=20 for a in ('claude','codex')):
                return ('Both quotas low - auto-resume when either recovers above 20%','yellow')
            return ('Handoff completed - checking agent eligibility','cyan')
        for name,s in [('Claude',c),('Codex',d)]:
            if s.get('state')=='prepare':return (name+' preparing a handoff (80% used)','yellow')
            if s.get('state')=='handoff':return (name+' at handoff threshold (90% used)','red')
        return ('Quota monitoring active - changes apply at the next handoff','green')

    def _lines(self, columns):
        import time
        self.last_poll=time.strftime('%H:%M:%S')
        bar_cells=14 if columns>=84 else (9 if columns>=65 else 5)
        try:
            version=Path(__file__).with_name('VERSION').read_text(encoding='utf-8').strip()
        except OSError:
            version='?'
        header=f'  AGENT WATCHDOG  v{version}       LIVE  •  {self.last_poll}'
        hr='  '+'─'*max(5,min(70,columns-4))
        rows=[(header,'cyan'),(hr,'dim')]
        for agent in ('claude','codex'):
            item=self.stats[agent]
            title='CLAUDE' if agent=='claude' else 'CODEX '
            stage=item.get('state','checking')
            for index,(key,label) in enumerate((('session','5H'),('weekly','7D'))):
                data=item.get('windows',{}).get(key)
                lead=f'  {title if index==0 else "      "}  {label}  '
                if item.get('online') is False:
                    line=lead+'? telemetry unavailable'
                    color='red'
                elif data:
                    used,left=data['used'],data['left']
                    reset_label=format_reset_local(data.get('reset_ts'), self.display_zone)
                    # Reset time must remain readable next to allowance, even
                    # in narrower terminals. Shrink the bar and suppress the
                    # less-important used percentage before clipping the reset.
                    allow_width=max(1,columns-1)
                    line=''
                    for bars,show_used in ((bar_cells,True),(max(5,bar_cells-5),True),
                                           (5,False),(0,False)):
                        usage_part=(f'{self._bar(used,bars)}  ' if bars else '')
                        usage_part+=(f'{used:5.1f}% used  ' if show_used else '')
                        candidate=lead+usage_part+f'{left:5.1f}% left  • {reset_label}'
                        line=candidate
                        if len(candidate)<=allow_width:
                            break
                    if len(line)>allow_width:
                        # Extreme terminal widths: prioritise quota and reset.
                        line=lead+f'{left:.0f}% left  • {reset_label}'
                    color='red' if left<=10 else ('yellow' if left<=20 else ('cyan' if index==0 else 'dim'))
                else:
                    line=lead+'waiting for measurements'
                    color='dim'
                rows.append((line,color))
            if 'complete' in stage: shown=stage.replace('handoff complete','handoff saved')
            elif stage=='normal': shown='ready / within limits'
            elif stage=='checking': shown='awaiting first reading'
            else: shown=stage
            rows.append((f'           {shown}', self._severity(stage)))
        next_text,next_color=self._pending()
        rows.extend([(hr,'dim'),('  NEXT  '+next_text,next_color),(hr,'dim')])
        chosen=saved()
        claude=chosen['claude'];codex=chosen['codex']
        hotkey='Shift+M models / effort' if self.available else 'models: -Command models'
        # On narrower windows, place model choices on another row. No wrapping
        # means the cursor-up count stays accurate after resizing.
        rows.extend([
            ('  '+hotkey+'   •   Ctrl+C stop','purple'),
            (f'  Claude {claude["model"]}/{claude["effort"]}   |   Codex {codex["model"]}/{codex["effort"]}','dim'),
        ])
        return [(line[:max(1,columns-1)],color) for line,color in rows]

    def _display(self):
        columns=max(24,shutil.get_terminal_size(fallback=(100,30)).columns)
        rows=self._lines(columns)
        if self.ansi_output:
            for text,color in rows:
                sys.stdout.write(self._colour(text,color)+'\n')
            sys.stdout.flush()
            self.live_lines=len(rows)
        else:
            signature=tuple((a,self.stats[a].get('state'),int(self.stats[a].get('left',-1)//10)) for a in ('claude','codex'))
            import time
            now=time.monotonic()
            if signature!=self.last_plain or now-self.last_plain_time>=300:
                for agent in ('claude','codex'):
                    s=self.stats[agent]
                    print(f'  {agent.upper():6s}  '+('  '.join(
                        f'{k}: {v["used"]:.0f}% used / {v["left"]:.0f}% left; {format_reset_local(v.get("reset_ts"),self.display_zone)}' for k,v in s['windows'].items()))+
                        f'  [{s.get("state","checking")}]',flush=True)
                print('  NEXT  '+self._pending()[0],flush=True)
                self.last_plain=signature
                self.last_plain_time=now
            if not self.fallback_footer_printed:
                print('  Shift+M models / effort (TTY)  •  use -Command models if redirected',flush=True)
                self.fallback_footer_printed=True

    def write_messages(self,messages,*,timestamp=None):
        self.clear_footer()
        events=self._extract(messages)
        import time
        label=timestamp if timestamp is not None else time.strftime('%H:%M:%S')
        now=time.monotonic()
        for message,severity in events:
            # Suppress repeated identical errors/events within 10 minutes, but
            # still log new findings, recoveries and lifecycle transitions.
            previous=self.recent_events.get('event_'+message,-float('inf'))
            if now-previous<600:continue
            self.recent_events['event_'+message]=now
            tag='ERROR' if severity=='red' else ('NOTICE' if severity=='yellow' else 'EVENT')
            line=f'  {label}  {tag:<6}  {message}'
            print(self._colour(line,severity),flush=True)
        if len(self.recent_events)>300:
            self.recent_events={k:v for k,v in self.recent_events.items()
                                if isinstance(v,(int,float)) and now-v<600 or k.startswith('last_offline_')}
        self._display()

    def show_footer(self):
        # The entire live section, including shortcuts, is the fixed footer.
        if not self.live_lines:self._display()
