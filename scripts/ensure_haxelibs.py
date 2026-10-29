#!/usr/bin/env python3
"""모드의 Project.xml 이 요구하는 haxelib 를 빌드 전에 설치하고, 실제로 설치됐는지 검증한다.

왜 필요한가
  GitHub Actions 실행 35414419153 이 'Could not find haxelib "flixel-ui"' 로 실패했다. 모드의 Project.xml 은 flixel-ui 를 선언했는데
  설치 단계가 haxelib 를 하나도 설치하지 못했고 아무 경고 없이 빌드로 넘어갔다. 원래 방식은 두 가지가 약했다.
    1) XML 파서가 거부하는 Project.xml(주석 안 '--', 이스케이프 안 된 '&' 등)이면 예외를 삼키고 설치를 통째로 건너뛴다.
       lime 은 관대한 자체 파서로 읽어서 이런 파일도 정상 빌드되므로 모드에서 흔하다.
    2) 설치 명령의 종료 코드를 보지 않고 '설치됐다'고 기록해, 실제로는 없어도 빌드를 시작한다.
  이 스크립트는 설치 뒤 'haxelib path' 로 실제 존재를 다시 확인하고, 필수 라이브러리가 끝내 없으면 이름을 밝히며 실패한다.

사용: python3 ensure_haxelibs.py <Project.xml 경로>
종료 코드: 0 = 필수 라이브러리 모두 사용 가능(또는 Project.xml 없음), 1 = 필수 라이브러리 누락
"""
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

# 모드가 선언하지 않아도 대부분의 FNF 모드가 쓰는 라이브러리. 설치 실패는 치명이 아니다(선언하지 않았다면 필요 없을 수 있다).
CORE_LIBS = ['lime', 'openfl', 'flixel', 'flixel-addons', 'flixel-ui', 'hxcpp', 'tjson', 'hscript', 'hxCodec', 'polymod', 'actuate', 'hxp', 'hxdiscord_rpc', 'newgrounds']
CORE_VERSIONS = {'tjson': '1.4.0'}

# html5 빌드에 필요 없는 플랫폼 이름. 조건에 이 이름들만 있으면 html5 에서는 쓰이지 않는다.
NON_WEB = {'desktop', 'switch', 'android', 'ios', 'cpp', 'windows', 'linux', 'mac', 'macos', 'hl', 'hashlink', 'neko', 'java', 'cs', 'python', 'lua', 'php', 'mobile', 'tvos', 'blackberry', 'air', 'flash', 'xbox', 'ps4', 'console'}
WEB = {'html5', 'web', 'js', 'javascript', 'emscripten', 'webgl', 'canvas', 'dom'}

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$')
VERSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.,\-]{0,40}$')
HAXELIB_TIMEOUT = 300


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name))


def valid_version(ver):
    return ver == '' or (isinstance(ver, str) and bool(VERSION_RE.match(ver)))


def _strip_comments(text):
    return re.sub(r'<!--.*?-->', '', text, flags=re.S)


def _local(tag):
    return tag.split('}')[-1] if isinstance(tag, str) else ''


def _from_parser(text):
    root = ET.fromstring(text.encode('utf-8'))
    out = []
    for el in root.iter():
        if _local(el.tag) == 'haxelib':
            out.append({'name': (el.get('name') or '').strip(), 'version': (el.get('version') or '').strip(),
                        'if': (el.get('if') or '').strip(), 'unless': (el.get('unless') or '').strip()})
    return out


_TAG_RE = re.compile(r'<haxelib\b([^>]*?)/?>', re.I | re.S)
_ATTR_RE = re.compile(r'([A-Za-z_:][\w:.\-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def _from_regex(text):
    out = []
    for m in _TAG_RE.finditer(_strip_comments(text)):
        attrs = {a.group(1).lower(): (a.group(2) if a.group(2) is not None else a.group(3)) for a in _ATTR_RE.finditer(m.group(1))}
        out.append({'name': attrs.get('name', '').strip(), 'version': attrs.get('version', '').strip(),
                    'if': attrs.get('if', '').strip(), 'unless': attrs.get('unless', '').strip()})
    return out


def find_declared_libs(text):
    """Project.xml 텍스트에서 haxelib 선언을 뽑는다. 파서가 거부하는 파일이면 정규식으로 다시 시도한다. 절대 예외를 내지 않는다."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        raw = _from_parser(text)
    except Exception:
        try:
            raw = _from_regex(text)
        except Exception:
            return []
    merged, order = {}, []
    for item in raw:
        if not item['name']:
            continue
        key = item['name'].lower()
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
        elif item['version'] and not merged[key]['version']:
            merged[key]['version'] = item['version']
    return [merged[k] for k in order]


def _tokens(cond):
    return [t for t in re.split(r'[^A-Za-z0-9_]+', cond.lower()) if t]


def is_required(lib):
    """html5 빌드에 필요한 선언인가. 모르는 조건은 필수로 본다(설치를 시도해서 해로울 것이 없다)."""
    cond, unless = lib.get('if', ''), lib.get('unless', '')
    if unless:
        t = _tokens(unless)
        if t and any(x in WEB for x in t):
            return False
    if not cond:
        return True
    t = _tokens(cond)
    if not t:
        return True
    if any(x in WEB for x in t):
        return True
    return not all(x in NON_WEB for x in t)


def _run(runner, cmd, timeout=HAXELIB_TIMEOUT):
    try:
        return runner(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # 바이너리 없음, 시간 초과 등 어떤 이유로든 호출자를 죽이지 않는다
        return subprocess.CompletedProcess(cmd, 127, '', '%s: %s' % (type(exc).__name__, exc))


def is_available(name, runner=subprocess.run):
    r = _run(runner, ['haxelib', 'path', name], timeout=30)
    text = ((r.stdout or '') + (r.stderr or '')).lower()
    if r.returncode != 0:
        return False
    return 'not installed' not in text and "run 'haxelib install" not in text


def _install(lib, runner):
    name, ver = lib['name'], lib.get('version', '')
    cmd = ['haxelib', 'install', name] + ([ver] if ver else []) + ['--quiet']
    r = _run(runner, cmd)
    if ver:
        _run(runner, ['haxelib', 'set', name, ver], timeout=30)
    return r


def ensure(libs, runner=subprocess.run, log=print):
    """libs 의 각 항목이 실제로 사용 가능하도록 한다. 반환: {'missing_required': [...], 'missing_optional': [...]}"""
    missing_required, missing_optional = [], []
    for lib in libs:
        name, ver = lib.get('name', ''), lib.get('version', '')
        if not valid_name(name) or not valid_version(ver):
            log('[ensure_haxelibs] 허용되지 않는 이름/버전이라 건너뜀: %r %r' % (name, ver))
            continue
        required = is_required(lib)
        if not ver and is_available(name, runner):
            log('[ensure_haxelibs] 이미 사용 가능: %s' % name)
            continue
        log('[ensure_haxelibs] 설치: %s%s' % (name, (' ' + ver) if ver else ' (latest)'))
        r = _install(lib, runner)
        if is_available(name, runner):
            log('[ensure_haxelibs] 확인됨: %s' % name)
            continue
        tail = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()[-1:] or ['']
        if required:
            log('[ensure_haxelibs] 필수 라이브러리를 설치하지 못했습니다: %s (%s)' % (name, tail[0][:160]))
            missing_required.append(name)
        else:
            log('[ensure_haxelibs] 선택 라이브러리 설치 실패(html5 빌드에는 필요 없음): %s (%s)' % (name, tail[0][:160]))
            missing_optional.append(name)
    return {'missing_required': missing_required, 'missing_optional': missing_optional}


def ensure_with_core(declared, runner=subprocess.run, log=print):
    """선언된 라이브러리를 먼저(버전 포함) 처리하고, 선언되지 않은 핵심 기본값을 채운다. 핵심 기본값의 실패는 치명이 아니다."""
    res = ensure(declared, runner, log)
    have = {l['name'].lower() for l in declared}
    core = [{'name': n, 'version': CORE_VERSIONS.get(n, ''), 'if': 'core_default_optional', 'unless': ''} for n in CORE_LIBS if n.lower() not in have]
    # 위 'if' 값은 is_required 가 모르는 조건이라 필수로 판정되므로, 핵심 기본값은 따로 선택 취급한다.
    core_res = ensure_optional(core, runner, log)
    res['missing_optional'] += core_res
    return res


def ensure_optional(libs, runner=subprocess.run, log=print):
    failed = []
    for lib in libs:
        name, ver = lib['name'], lib.get('version', '')
        if not valid_name(name) or not valid_version(ver):
            continue
        if not ver and is_available(name, runner):
            continue
        _install(lib, runner)
        if not is_available(name, runner):
            log('[ensure_haxelibs] 기본 라이브러리 설치 실패(모드가 선언하지 않았으면 무시): %s' % name)
            failed.append(name)
    return failed


def main(argv):
    path = argv[1] if len(argv) > 1 else 'Project.xml'
    if not os.path.isfile(path):
        print('[ensure_haxelibs] Project.xml 을 찾지 못해 검증을 건너뜁니다: %s' % path)
        return 0
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except OSError as exc:
        print('[ensure_haxelibs] Project.xml 을 읽지 못해 검증을 건너뜁니다: %s' % exc)
        return 0
    declared = find_declared_libs(text)
    print('[ensure_haxelibs] Project.xml 선언 %d개: %s' % (len(declared), ', '.join(l['name'] for l in declared) or '(없음)'))
    res = ensure_with_core(declared)
    if res['missing_required']:
        print('::error::Project.xml 이 요구하는 haxelib 를 설치하지 못했습니다: %s' % ', '.join(res['missing_required']))
        return 1
    print('[ensure_haxelibs] 필수 haxelib 모두 사용 가능')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
