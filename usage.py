"""Fail-safe parsing of local Claude/Codex usage API responses (stdlib only)."""
from __future__ import annotations
import json
import re
import urllib.request
from dataclasses import dataclass

VALID_WINDOWS = ('session','five_hour','5_hour','5h','current','weekly','week','seven_day','7_day','7d','rolling','primary','secondary')
EXCLUDE = ('reset','timestamp','updated','token','count','seconds','minutes','hours','date','time','cost','price','dollar','request','http','status_code')


@dataclass(frozen=True)
class Metric:
    path: str
    used_percent: float
    label: str


def _norm(s):
    return re.sub(r'[^a-z0-9]+', '_', str(s).lower()).strip('_')


def _numeric(value):
    if isinstance(value, bool): return None
    if isinstance(value, (int, float)): return float(value)
    if isinstance(value, str):
        t = value.strip().replace('%', '')
        if re.fullmatch(r'\d+(?:\.\d+)?', t): return float(t)
    return None


def _safe_pct(value):
    n = _numeric(value)
    if n is not None and 0 <= n <= 100: return round(n, 3)
    return None


def _is_usage_path(path):
    parts = [_norm(i) for i in path.split('.')]
    return any(any(window in part for window in VALID_WINDOWS) for part in parts) or 'usage' in parts


def _extract(obj, path='$'):
    out = []
    if isinstance(obj, dict):
        normalized = {_norm(k): v for k, v in obj.items()}
        # A well-formed usage window with used/limit or remaining/limit.
        limit = next((_numeric(normalized[k]) for k in ('limit','total','capacity','max','maximum','quota') if k in normalized), None)
        used = next((_numeric(normalized[k]) for k in ('used','consumed','usage') if k in normalized), None)
        rem = next((_numeric(normalized[k]) for k in ('remaining','left','available') if k in normalized), None)
        if _is_usage_path(path) and limit is not None and limit > 0:
            if used is not None and 0 <= used <= limit:
                out.append(Metric(path,round(100*used/limit,3), 'used/limit'))
            elif rem is not None and 0 <= rem <= limit:
                out.append(Metric(path,round(100*(1-rem/limit),3),'remaining/limit'))
        for original, value in obj.items():
            key = _norm(original)
            child = f'{path}.{original}'
            if isinstance(value, (dict, list)):
                out.extend(_extract(value, child))
                continue
            if any(bad in key for bad in EXCLUDE): continue
            val = _numeric(value)
            if val is None: continue
            pct = None
            if ('percent' in key or 'pct' in key or isinstance(value,str) and '%' in value):
                if any(w in key for w in ('remaining','left','available','unused','free')):
                    n = _safe_pct(val)
                    pct = None if n is None else 100-n
                elif any(w in key for w in ('used','usage','utiliz','consum','session','weekly','week','five_hour','fivehour','5h')) or _is_usage_path(path):
                    pct = _safe_pct(val)
            elif 'utilization' in key or 'utilisation' in key:
                # Claude rate-limit utilization is a fraction; percent is also accepted.
                pct = _safe_pct(val*100 if 0 <= val <= 1 else val)
            if pct is not None and (_is_usage_path(path) or _is_usage_path(child)):
                out.append(Metric(child,round(pct,3),'percentage'))
    elif isinstance(obj, list):
        for i, val in enumerate(obj): out.extend(_extract(val,f'{path}.{i}'))
    return out


def metrics_from_json(obj, path=None):
    """Returns trusted percentage measurements; does NOT guess from bare numbers."""
    if path:
        cur = obj
        for part in path.strip('$.').split('.'):
            cur = cur[int(part)] if isinstance(cur,list) else cur[part]
        n = _safe_pct(cur)
        if n is None: raise ValueError('configured used_pct_path is not a 0..100 percentage')
        return [Metric(path,n,'explicit configured used percentage')]
    all_metrics = _extract(obj)
    # Deduplicate within path to avoid double counting both derived and percentage.
    unique = {(m.path,m.used_percent):m for m in all_metrics}
    return list(unique.values())


def window_name(path):
    """Identify the quota window from trustworthy path names, not from percentages."""
    nodes = [_norm(x) for x in path.split('.')]
    session = {'session', 'current', 'current_session', 'five_hour', 'fivehour', '5_hour', '5h', 'rolling', 'primary'}
    weekly = {'weekly', 'week', 'seven_day', 'sevenday', '7_day', '7d', 'secondary'}
    # Prefer the closest enclosing named window, rather than a broad ancestor.
    for node in reversed(nodes):
        if node in session or node.startswith('current_session'): return 'session'
        if node in weekly or node.startswith('weekly_'): return 'weekly'
    return 'other'


def summarise_metrics(metrics):
    """Expose all recognized quota windows; apply thresholds to the tightest one."""
    windows = {}
    for m in metrics:
        name = window_name(m.path)
        candidate = {'used_percent':m.used_percent,
                     'remaining_percent':round(100-m.used_percent,3),
                     'metric':m.path}
        if name not in windows or candidate['used_percent'] > windows[name]['used_percent']:
            windows[name] = candidate
    return windows


def get_api_usage(url, timeout=1.5, used_pct_path=None):
    if not url.startswith('http://127.0.0.1:') and not url.startswith('http://localhost:'):
        raise ValueError('Only local loopback HTTP usage URLs are permitted')
    req = urllib.request.Request(url,headers={'Accept':'application/json', 'User-Agent':'agent-handoff-watchdog/1.0'})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        if (resp.headers.get('content-length') or '').isdigit() and int(resp.headers['content-length'])>100000:
            raise ValueError('usage API response too large')
        raw=resp.read(100001)
    if len(raw)>100000: raise ValueError('usage API response too large')
    obj=json.loads(raw)
    metrics=metrics_from_json(obj,used_pct_path)
    if not metrics:
        raise ValueError('Cannot safely identify a percentage in usage JSON. Run inspect and configure used_pct_path.')
    max_metric=max(metrics,key=lambda x:x.used_percent)
    windows=summarise_metrics(metrics)
    # Timestamps are display-only. They must NEVER affect quota stage decisions.
    from reset_times import resets_from_json
    resets=resets_from_json(obj, windows)
    for name, instant in resets.items():
        windows[name]['reset_ts']=instant
    return {'used_percent':max_metric.used_percent,'remaining_percent':round(100-max_metric.used_percent,3),
            'limiting_metric':max_metric.path, 'limiting_window':window_name(max_metric.path),
            'windows':windows,
            'metrics':[{'path':x.path,'used_percent':x.used_percent} for x in metrics]}


def paths_only(obj, prefix='$', depth=0):
    """Inspect only JSON structure (no tokens, addresses, secrets, raw values)."""
    if depth>6:return []
    if isinstance(obj,dict):
        out=[]
        for key,val in obj.items():
            node=f'{prefix}.{key}'
            out.append((node,type(val).__name__))
            out.extend(paths_only(val,node,depth+1))
        return out
    if isinstance(obj,list):
        return paths_only(obj[0],prefix+'.0',depth+1) if obj else []
    return []
