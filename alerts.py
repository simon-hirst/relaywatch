"""Low-noise user-input alerts: Windows desktop + optional ntfy Android push."""
from __future__ import annotations
import hashlib
import json
import os
import platform
import shutil
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from engine import atomic_write_json, load_json

MAX_QUESTION = 500
# Before v3.10, auto-continuation's stalled-progress warning was recorded
# indistinguishably from a genuine question. This exact prefix supports safe
# upgrades for existing projects without discarding real user decisions.
AUTO_STALL_PREFIX = 'The agent stopped after repeated continuations without updating the handoff.'


def request_path(root):
    return Path(root)/'.agent-watchdog'/'needs-input.json'

def recording_key(item):
    return hashlib.sha256(json.dumps({k:item.get(k) for k in ('agent','question','created_at')},sort_keys=True).encode()).hexdigest()[:24]

def register_question(root, agent, question, *, now=None, source="agent"):
    """Record an explicitly marked blocking user question from an agent's Stop output."""
    question = re.sub(r'\s+', ' ', str(question)).strip()[:MAX_QUESTION]
    if not question: return False
    path = request_path(root)
    prev = load_json(path,{}) or {}
    if prev.get('status')=='open' and prev.get('agent')==agent and prev.get('question')==question:
        return False
    data={'agent':agent,'question':question,'status':'open','source':source,'created_at':time.time() if now is None else now}
    atomic_write_json(path,data)
    return True

def is_auto_stall(item):
    """Identify *only* the watchdog's own no-progress alert, old or new."""
    if not isinstance(item, dict) or item.get('status') != 'open':
        return False
    text = str(item.get('question') or '')
    return (item.get('source') == 'watchdog_auto_continue_stall'
            and text.startswith(AUTO_STALL_PREFIX)) or (
                not item.get('source') and text.startswith(AUTO_STALL_PREFIX))


def dismiss_auto_stall(root, agent):
    """A deliberate CLI resume may acknowledge only a watchdog-generated stall."""
    path = request_path(root)
    item = load_json(path, {}) or {}
    if item.get('agent') != agent or not is_auto_stall(item):
        return False
    atomic_write_json(path, {**item, 'status':'acknowledged',
                             'acknowledged_by':'explicit_cli_resume',
                             'acknowledged_at':time.time()})
    return True


def pending_question(root):
    item = load_json(request_path(root),{}) or {}
    return item if item.get('status') == 'open' and item.get('question') else {}

def supersede_handed_off_question(root, source, handoff_created_at):
    """Archive only an older decision from a provider that has finished handing off.

    Do not interpret the handoff as approval. Preserve the question in place for
    auditability. Newer decisions and questions from other agents remain open.
    """
    path = request_path(root)
    item = load_json(path, {}) or {}
    created = item.get('created_at')
    if (item.get('status') != 'open' or item.get('agent') != source
            or not isinstance(created, (int, float))
            or not isinstance(handoff_created_at, (int, float))
            or created > handoff_created_at):
        return False
    atomic_write_json(path, {**item, 'status': 'superseded',
                             'superseded_at': time.time(),
                             'superseded_reason': 'Source provider completed a verified handoff; no permission granted'})
    return True


# A formal one-line marker is the preferred agent/user-decision protocol. The
# fallback is deliberately narrow: only an unmistakable assistant-addressed
# decision heading followed by a genuine question, not any question mark in a
# test result, quoted log, or routine status update.
FORMAL_QUESTION = re.compile(
    r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?(?P<tag>NEEDS_USER_INPUT|USER_DECISION_REQUEST)"
    r"(?:\*\*)?\s*[:\-]\s*(?P<question>.+?)\s*$"
)
DECISION_HEADING = re.compile(
    r"(?i)^(?:decision\s+for\s+you|question\s+for\s+you|"
    r"approval\s+(?:request|needed|required)|permission\s+(?:request|needed|required))\s*:\s*(.*)$"
)


def detect_question(text):
    """Return (question, origin), or (None, None).

    The native Stop payload contains the final assistant message, not the
    conversation transcript. Require an intentional heading for the fallback
    to avoid treating incidental/rhetorical questions as approval requests.
    """
    if not isinstance(text, str):
        return None, None
    lines = text.splitlines()
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(('```', '~~~')):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(('>', '|')):
            continue
        formal = FORMAL_QUESTION.match(line)
        if formal:
            value = formal.group('question').strip()[:MAX_QUESTION]
            if value:
                return value, 'agent_formal'
    # Never parse fenced code/log examples as fresh approval requests.
    fenced = False
    for index, original in enumerate(lines):
        stripped = original.strip()
        if stripped.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or stripped.startswith(('>', '|')):
            continue
        # Markdown bold headings are common in Claude's final response.
        line = re.sub(r'\*\*', '', stripped)
        line = re.sub(r'^[-*]\s+', '', line)
        match = DECISION_HEADING.match(line)
        if not match:
            continue
        question = match.group(1)
        # Allow one wrapped question, never absorb the entire response.
        for continuation in lines[index + 1:index + 4]:
            if '?' in question or not continuation.strip():
                break
            if continuation.lstrip().startswith(('```', '>', '-', '*', '#')):
                break
            question += ' ' + continuation.strip()
        if '?' not in question:
            continue
        question = re.sub(r'\s+', ' ', question.split('?', 1)[0].strip() + '?')
        if len(question) < 10:
            continue
        return question[:MAX_QUESTION], 'agent_informal_decision'
    return None, None


def extract_question(text):
    """Compatibility helper for explicit markers and unambiguous decisions."""
    return detect_question(text)[0]


def is_automatic_continuation(prompt):
    """A watchdog-injected prompt or generic `continue` is not an answer.

    Genuine user yes/no/approval responses remain able to acknowledge an open
    decision, but a CLI resume must never silently grant operational approval.
    """
    if not isinstance(prompt, str):
        return True
    text = prompt.strip().lower()
    return (not text or text in ('continue', 'resume', 'keep going', 'continue with next task')
            or text.startswith(('user-requested resume.', 'automatic handoff continuation.',
                                'continue with next task. automatic workflow continuation.',
                                'continue with next task. read docs/handoff.md')))

def clear_question(root,agent):
    path=request_path(root)
    value=load_json(path,{}) or {}
    if value.get('status')=='open' and value.get('agent')==agent:
        value.update({'status':'answered','answered_at':time.time()})
        atomic_write_json(path,value)
        return True
    return False

def _windows_desktop(root, title, message, *,popen=subprocess.Popen,run=subprocess.run):
    script=Path(root)/'.agent-watchdog'/'desktop-alert.ps1'
    system = platform.system()
    if system == 'Darwin':
        # Pass untrusted user-facing notification text as osascript arguments,
        # never embedded in AppleScript source.
        code = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
        try:
            cp = run(['osascript','-e',code,title[:120],message[:600]],
                     capture_output=True,text=True,timeout=6)
            return 'sent' if cp.returncode == 0 else 'desktop unavailable: '+(cp.stderr or '')[:180]
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'desktop unavailable: {type(exc).__name__}: {exc}'
    if system == 'Linux' and not (os.environ.get('WSL_DISTRO_NAME') or
                                  'microsoft' in os.uname().release.lower()):
        if not shutil.which('notify-send'):
            return 'desktop unavailable: install libnotify / notify-send'
        try:
            cp = run(['notify-send','--app-name=RelayWatch',title[:120],message[:600]],
                     capture_output=True,text=True,timeout=6)
            return 'sent' if cp.returncode == 0 else 'desktop unavailable: '+(cp.stderr or '')[:180]
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'desktop unavailable: {type(exc).__name__}: {exc}'
    if system != 'Linux':
        return 'desktop unavailable: use Windows WSL2, Linux or macOS'
    if not script.is_file():return 'missing desktop-alert.ps1; reinstall watchdog'
    try:
        converted=run(['wslpath','-w',str(script)],capture_output=True,text=True,timeout=4)
        if converted.returncode or not converted.stdout.strip():return 'wslpath unavailable (not running in WSL?)'
        popen(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',
               converted.stdout.strip(),'-Title',title[:120],'-Message',message[:600]],
              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
              start_new_session=True)
        return 'sent'
    except (OSError,subprocess.TimeoutExpired) as exc:
        return f'desktop unavailable: {type(exc).__name__}: {exc}'

def _phone_push(cfg,title,message,*,opener=urllib.request.urlopen):
    phone=cfg.get('phone',{})
    if not phone.get('enabled',False):return 'disabled'
    topic=phone.get('topic','')
    # Validate destination to avoid posting to untrusted URLs via config mixups.
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',topic):return 'invalid topic'
    server=phone.get('server','https://ntfy.sh').rstrip('/')
    if not server.startswith('https://') or '/' in server[len('https://'):]:return 'invalid server'
    url=server+'/'+topic
    req=urllib.request.Request(url,method='POST',data=message.encode('utf-8'),
                              headers={'Title':title,'Priority':'high','Tags':'warning'})
    try:
        with opener(req,timeout=6) as resp:
            if not (200<=resp.status<300):return f'HTTP {resp.status}'
        return 'sent'
    except (urllib.error.URLError,OSError,TimeoutError) as exc:
        return f'phone unavailable: {type(exc).__name__}: {exc}'

def send_event(root,config,title,desktop_message,phone_message,*,desktop_sender=_windows_desktop,phone_sender=_phone_push):
    settings=config.get('alerts',{})
    result={}
    if settings.get('desktop',True):result['desktop']=desktop_sender(root,title,desktop_message)
    else:result['desktop']='disabled'
    if settings.get('phone',{}).get('enabled',False):result['phone']=phone_sender(settings,title,phone_message)
    else:result['phone']='disabled'
    return result

def notify_pending_question(root,config,*,now=None,desktop_sender=_windows_desktop,phone_sender=_phone_push):
    item=load_json(request_path(root),{}) or {}
    if item.get('status')!='open' or item.get('agent') not in ('claude','codex') or not item.get('question'):
        return None
    now=time.time() if now is None else now
    key=recording_key(item)
    path=Path(root)/'.agent-watchdog'/'alert-delivery.json'
    ledger=load_json(path,{}) or {}
    record=ledger.get(key,{})
    stall=is_auto_stall(item)
    title=(f'Watchdog: {item["agent"].capitalize()} stopped making progress' if stall
           else f'Agent needs input: {item["agent"].capitalize()}')
    question=re.sub(r'\s+',' ',str(item['question']))[:MAX_QUESTION]
    desktop_body=('Stalled workflow: '+question if stall else 'Question: '+question+' — Respond in the active agent session.')
    settings=config.get('alerts',{})
    phone_body=(question if settings.get('phone',{}).get('include_question',False)
                else ('Agent stopped making progress; review the active session.' if stall
                      else 'A question is blocking progress. Open the active agent session to reply.'))
    results={}
    channels={'desktop':settings.get('desktop',True),'phone':settings.get('phone',{}).get('enabled',False)}
    for channel,enabled in channels.items():
        if not enabled:continue
        last=record.get(channel,{})
        if last.get('sent') or now-last.get('attempted_at',0)<max(30,config.get('alert_retry_seconds',120)):
            continue
        result=(desktop_sender(root,title,desktop_body) if channel=='desktop'
                else phone_sender(settings,title,phone_body))
        record[channel]={'sent':result=='sent','attempted_at':now,'result':result}
        results[channel]=result
    if results:
        ledger[key]=record
        # Avoid uncontrolled accumulation if questions change frequently.
        if len(ledger)>100:
            ledger=dict(list(ledger.items())[-100:])
        atomic_write_json(path,ledger)
        return (f'AGENT STALLED: {item["agent"]}' if stall else f'NEEDS USER INPUT from {item["agent"]}') + ': '+', '.join(k+'='+v for k,v in results.items())
    return None
