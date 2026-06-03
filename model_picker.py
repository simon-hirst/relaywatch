#!/usr/bin/env python3
"""Interactive runtime model selection for watchdog-managed Claude and Codex.

Codex: authenticated model/list from installed Codex app-server.
Claude: current public Claude Code model list from Anthropic Help Center.
Cache last successful model catalog and retain offline fallback; user can enter a
custom ID, since model visibility is controlled by the account and CLI version.
"""
from __future__ import annotations

import argparse
try:
    import curses
except ImportError:
    curses = None  # Debian minimal Python: fall back to numbered terminal menu.
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / '.agent-watchdog' / 'models.json'
CACHE = ROOT / '.agent-watchdog' / 'model-catalog-cache.json'
CLAUDE_URL = 'https://support.claude.com/en/articles/11940350-claude-code-model-configuration'
EFFORT_URL = 'https://code.claude.com/docs/en/model-config'
# IDs are deliberately restricted so they cannot inject shell code or flags.
MODEL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{1,100}$')
CODEX_FALLBACK = [
    ('gpt-6.1-sol', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
    ('gpt-6-astra', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
    ('gpt-6-sol', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
    ('gpt-6-luna', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.6-sol', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
    ('gpt-5.6-terra', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
    ('gpt-5.6-luna', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.5', ['low', 'medium', 'high', 'xhigh']),
]
CLAUDE_FALLBACK = [
    'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5',
    'claude-fable-5-1', 'claude-opus-5', 'claude-sonnet-5',
    'claude-fable-5', 'claude-opus-4-8', 'claude-opus-4-7',
    'claude-sonnet-4-6', 'claude-opus-4-6'
]
DEFAULTS = {'claude': {'model': 'claude-opus-5-5', 'effort': 'xhigh'},
            'codex': {'model': 'gpt-6.1-sol', 'effort': 'xhigh'}}


def is_model_id(value):
    return isinstance(value, str) and bool(MODEL_ID.fullmatch(value))


def effort_list(agent, model):
    """Conservative fallback from published Claude docs; Codex from live catalog."""
    if agent == 'codex':
        return ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
    if re.search(r'-(4-6)$', model):
        return ['low', 'medium', 'high', 'max']
    if re.search(r'-(4-[0-5])(?:-|$)', model):
        return ['default']
    return ['low', 'medium', 'high', 'xhigh', 'max']


def entries_fallback(agent):
    if agent == 'claude':
        return [{'id': m, 'label': m.replace('claude-', '').replace('-', ' ').title(),
                 'efforts': effort_list(agent, m)} for m in CLAUDE_FALLBACK]
    return [{'id': m, 'label': m, 'efforts': e} for m, e in CODEX_FALLBACK]


def normalize_codex(raw):
    """Use ONLY visible authenticated app-server results, including allowed efforts."""
    rows = raw.get('data', []) if isinstance(raw, dict) else []
    items = []
    for obj in rows:
        if not isinstance(obj, dict) or obj.get('hidden') is True:
            continue
        mid = obj.get('model') or obj.get('id')
        if not is_model_id(mid):
            continue
        efforts = []
        for item in obj.get('supportedReasoningEfforts', []) or []:
            val = item.get('reasoningEffort') if isinstance(item, dict) else item
            if val in ('low','medium','high','xhigh','max','ultra','minimal','none') and val not in efforts:
                efforts.append(val)
        if not efforts:
            efforts = effort_list('codex', mid)
        items.append({'id': mid, 'label': (obj.get('displayName') or mid), 'efforts': efforts})
    return items


def codex_models_from_cli(timeout=8):
    """Authenticated Codex app-server JSON-RPC, no thread or inference spawned."""
    if not shutil.which('codex'):
        raise RuntimeError('codex binary is not on PATH')
    proc = subprocess.Popen(['codex','app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    reader = selectors.DefaultSelector()
    buffer = b''
    deadline = time.monotonic() + timeout
    try:
        reader.register(proc.stdout, selectors.EVENT_READ)
        init = {'method':'initialize','id':1,'params':{
            'clientInfo':{'name':'agent_handoff_watchdog','title':'Agent Handoff Watchdog','version':'1.9.0'}}}
        proc.stdin.write((json.dumps(init)+'\n').encode())
        proc.stdin.flush()
        requested = False
        models = []
        cursor = None
        while time.monotonic() < deadline:
            ready = reader.select(timeout=max(.01, deadline-time.monotonic()))
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 32768)
            if not chunk:
                break
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    packet = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if packet.get('id') == 1:
                    if 'error' in packet:
                        raise RuntimeError('Codex app-server rejected initialization')
                    proc.stdin.write((json.dumps({'method':'initialized','params':{}})+'\n').encode())
                    proc.stdin.write((json.dumps({'method':'model/list','id':2,'params':{'limit':100,'includeHidden':False}})+'\n').encode())
                    proc.stdin.flush()
                    requested = True
                elif packet.get('id') == 2:
                    if 'error' in packet:
                        raise RuntimeError('Codex model/list rejected: '+str(packet['error'])[:160])
                    result = packet.get('result') or {}
                    models.extend(normalize_codex(result))
                    cursor = result.get('nextCursor')
                    if not cursor:
                        return models
                    # Paginated list of models, same request id allowed sequentially.
                    proc.stdin.write((json.dumps({'method':'model/list','id':2,'params':{'limit':100,'includeHidden':False,'cursor':cursor}})+'\n').encode())
                    proc.stdin.flush()
        raise RuntimeError('Codex app-server did not return model/list in time')
    finally:
        reader.close()
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        proc.stdin.close()
        proc.stdout.close()


def claude_models_online(timeout=5):
    request = urllib.request.Request(CLAUDE_URL, headers={'User-Agent':'Mozilla/5.0 AgentHandoffWatchdog/1.9'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(1_500_000).decode('utf-8', 'replace')
    models = []
    for m in re.findall(r'claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)*(?:-\d{8})?', data):
        if m not in models and is_model_id(m): models.append(m)
    if not models:
        raise RuntimeError('No supported model IDs discovered on official Claude help page')
    return [{'id':m, 'label':m.replace('claude-','').replace('-',' ').title(),
             'efforts':effort_list('claude',m)} for m in models]


def read_json(path, fallback=None):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):return fallback


def atomic_json(path, data):
    path.parent.mkdir(exist_ok=True,parents=True)
    tmp = path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    os.replace(tmp,path)


def saved():
    result = read_json(DATA,{}) or {}
    return {a: {'model': result.get(a,{}).get('model',DEFAULTS[a]['model']),
                'effort': result.get(a,{}).get('effort',DEFAULTS[a]['effort'])}
            for a in ('claude','codex')}


def save(choices):
    for agent in ('claude','codex'):
        model = choices[agent]['model']; effort=choices[agent]['effort']
        if not is_model_id(model) or effort not in ('default','low','medium','high','xhigh','max','ultra','none','minimal'):
            raise ValueError(f'Invalid {agent} model/effort selection')
    atomic_json(DATA, {a:{**choices[a]} for a in ('claude','codex')})


def catalogs(refresh=True):
    cache = read_json(CACHE,{}) or {}
    found = {}
    sources = {}
    for agent, get in [('claude',claude_models_online), ('codex',codex_models_from_cli)]:
        data = None
        if refresh:
            try:
                data = get()
                if data:
                    cache[agent]={'items':data, 'updated':int(time.time()),
                                  'source':'Anthropic website' if agent=='claude' else 'Authenticated Codex app-server'}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                sources[agent] = f'Online lookup unavailable ({type(exc).__name__})'
        if not data:
            record = cache.get(agent,{})
            data = record.get('items')
            if data:
                sources[agent] = 'cached '+record.get('source','catalog')
            else:
                data = entries_fallback(agent)
                sources[agent] = 'bundled offline fallback (model availability unverified)'
        else:
            sources[agent] = cache[agent]['source']
        # Always include previously selected custom/older IDs, but mark them.
        existing = saved()[agent]['model']
        if is_model_id(existing) and all(d['id'] != existing for d in data):
            data.insert(0, {'id':existing,'label':existing+' (saved, not in current list)',
                            'efforts':effort_list(agent,existing)})
        found[agent] = data
    if refresh:
        atomic_json(CACHE,cache)
    return found,sources


def choose_text(agent, kind, options, current, source):
    while True:
        print(f'\n  {agent.upper()} — Choose {kind}  [{source}]')
        for i,item in enumerate(options,1):
            label=item['label'] if isinstance(item,dict) else item
            key=item['id'] if isinstance(item,dict) else item
            print(f'  {i:>2}. {label}'+('   [current]' if key==current else ''))
        extra = ', or c for custom ID' if kind=='model' else ''
        raw=input(f'  Number (Enter keeps {current}{extra}, q cancels): ').strip()
        if raw.lower()=='q':raise KeyboardInterrupt
        if not raw:
            return current
        if kind=='model' and raw.lower()=='c':
            custom = input('  Custom model ID: ').strip()
            if is_model_id(custom):return custom
            print('  Invalid model ID')
            continue
        if raw.isdigit() and 1<=int(raw)<=len(options):
            value=options[int(raw)-1]
            return value['id'] if isinstance(value,dict) else value
        print('  Choose a valid number.')


def popup(stdscr,title,subtitle,items,selected,custom=False):
    curses.curs_set(0)
    curses.use_default_colors()
    if curses.has_colors():
        curses.init_pair(1,curses.COLOR_CYAN,-1)
        curses.init_pair(2,curses.COLOR_GREEN,-1)
        curses.init_pair(3,curses.COLOR_YELLOW,-1)
    cursor=next((i for i,entry in enumerate(items) if (entry['id'] if isinstance(entry,dict) else entry)==selected),0)
    query=''; scroll=0
    while True:
        stdscr.erase()
        height,width=stdscr.getmaxyx()
        if height<12 or width<48:
            stdscr.addnstr(0,0,'Increase terminal size (min 48x12). Esc cancels.',max(width-1,1))
            stdscr.refresh()
            key=stdscr.getch()
            if key in (27,ord('q')):raise KeyboardInterrupt
            continue
        def text(y,x,value,style=0):
            if y < height and x < width:
                try:stdscr.addnstr(y,x,str(value),max(0,width-x-1),style)
                except curses.error:pass
        text(1,3,'AGENT HANDOFF  /  MODEL SETUP',curses.A_BOLD | curses.color_pair(1))
        text(3,3,title,curses.A_BOLD)
        text(4,3,subtitle,curses.color_pair(2))
        text(6,3,'Search: '+(query or 'type to filter models'))
        filtered=[item for item in items if query.lower() in (item['id']+' '+item['label'] if isinstance(item,dict) else item).lower()]
        if filtered:
            cursor=min(cursor,len(filtered)-1)
            visible=max(1,height-11)
            if cursor<scroll:scroll=cursor
            if cursor>=scroll+visible:scroll=cursor-visible+1
            for line,item in enumerate(filtered[scroll:scroll+visible]):
                idx=scroll+line
                value=item['id'] if isinstance(item,dict) else item
                name=item['label'] if isinstance(item,dict) else item
                txt=('  > ' if idx==cursor else '    ')+name
                style=(curses.A_REVERSE | curses.A_BOLD) if idx==cursor else (curses.color_pair(2) if value==selected else 0)
                text(8+line,3,txt,style)
        else:text(8,5,'No matches; Backspace to clear search.')
        text(height-3,3,'Up/Down choose  Enter select  / search  Backspace erase')
        text(height-2,3,'Esc cancel'+('    C custom model ID' if custom else ''))
        stdscr.refresh()
        key=stdscr.getch()
        if key in (27,3):raise KeyboardInterrupt
        if key in (curses.KEY_UP,ord('k')):
            if filtered:cursor=(cursor-1)%len(filtered)
        elif key in (curses.KEY_DOWN,ord('j')):
            if filtered:cursor=(cursor+1)%len(filtered)
        elif key in (10,13,curses.KEY_ENTER):
            if filtered:
                value=filtered[cursor]
                return value['id'] if isinstance(value,dict) else value
        elif key in (curses.KEY_BACKSPACE,127,8):
            query=query[:-1];cursor=0;scroll=0
        elif custom and key in (ord('c'),ord('C')) and not query:
            curses.echo();curses.curs_set(1)
            text(height-4,3,'Model ID: ')
            stdscr.refresh()
            try:val=stdscr.getstr(height-4,13,100).decode('utf-8').strip()
            finally:curses.noecho();curses.curs_set(0)
            if is_model_id(val):return val
        elif key==ord('/'):
            query='';cursor=0;scroll=0
        elif 32<=key<127:
            query+=chr(key);cursor=0;scroll=0


def choose_all(models, sources, choices, *, use_curses=True):
    for agent in ('claude','codex'):
        entries=models[agent]
        current=choices[agent]['model']
        if use_curses:
            model=curses.wrapper(popup, f'{agent.upper()} / Select model', sources[agent], entries,current,True)
        else:model=choose_text(agent,'model',entries,current,sources[agent])
        matching=next((e for e in entries if e['id']==model),None)
        opts=matching['efforts'] if matching else effort_list(agent,model)
        current_effort=choices[agent]['effort']
        if current_effort not in opts:current_effort=opts[-1]
        if use_curses:
            effort=curses.wrapper(popup, f'{agent.upper()} / Effort for {model}', 'Supported options for selected model',opts,current_effort,False)
        else:effort=choose_text(agent,'effort',opts,current_effort,'supported effort options')
        choices[agent]={'model':model,'effort':effort}
    return choices


def main():
    parser=argparse.ArgumentParser(description='Select runtime Claude and Codex models and effort')
    parser.add_argument('command',nargs='?',choices=('choose','print','show'),default='choose')
    parser.add_argument('--agent',choices=('claude','codex'))
    parser.add_argument('--text',action='store_true',help='numbered non-curses selector')
    parser.add_argument('--offline',action='store_true',help='skip live model discovery')
    args=parser.parse_args()
    chosen=saved()
    if args.command=='print':
        a=args.agent
        if not a:parser.error('--agent required for print')
        e=chosen[a]
        if not is_model_id(e['model']) or e['effort'] not in ('default','low','medium','high','xhigh','max','ultra','none','minimal'):
            parser.error('invalid saved model selection')
        print(e['model']+'\t'+e['effort'])
        return
    if args.command=='show':
        for a in ('claude','codex'):
            print(f'{a.capitalize():<7} {chosen[a]["model"]} / {chosen[a]["effort"]}')
        return
    if not sys.stdin.isatty():
        parser.error('Interactive model picker needs a terminal. Use "show" or run in PowerShell/WSL console.')
    print('\n  Loading model lists (Codex from your signed-in CLI, Claude from Anthropic)...',flush=True)
    catalogs_data,sources=catalogs(refresh=not args.offline)
    try:
        edited=choose_all(catalogs_data,sources,chosen,use_curses=not args.text and curses is not None and sys.stdout.isatty() and bool(os.environ.get('TERM')))
    except (KeyboardInterrupt,EOFError):
        print('\n  Cancelled. Previous model choices unchanged.')
        return 2
    print('\n'+'='*54)
    for a in ('claude','codex'):
        print(f'  {a.upper():<7} {edited[a]["model"]} / {edited[a]["effort"]}')
    print('  Saved live: next handoff/agent launch uses these settings without restarting the watchdog.')
    if input('\n  Save these launch settings? [Y/n] ').strip().lower() not in ('','y','yes'):
        print('  Cancelled.');return 2
    save(edited)
    print('  Saved! The running watchdog uses these for its NEXT handoff. No restart required.')
    return 0

if __name__=='__main__':sys.exit(main())
