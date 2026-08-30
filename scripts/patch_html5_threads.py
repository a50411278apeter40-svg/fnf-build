#!/usr/bin/env python3
"""
[버그 수정] html5(JS) 빌드 컴파일 에러 근본 해결.
  A) sys.thread.* / sys.io.* / sys.FileSystem 을 조건 없이 쓰는 엔진 코드
  B) "데스크톱 전용 기능인데 define이 무조건 켜져있어서" 딸려 들어오는
     플랫폼 제한 라이브러리(예: hxdiscord_rpc)

겪은 사례들 (전부 같은 근본 패턴 — 엔진/모드 소스가 데스크톱을 기본
가정하고 만들어져서, html5 타겟 조건을 빠뜨린 채 sys 패키지나 네이티브
전용 라이브러리를 "무조건" 참조함):

  1) sys/thread/FixedThreadPool.hx: This class is not available on this target
     → LoadingState.hx/Discord.hx가 sys.thread.FixedThreadPool/Mutex/Thread를
       #if 없이 무조건 import+사용.
  2) hxdiscord_rpc/Discord.hx: 'Discord RPC supports only C++ target platforms.'
     → Project.xml의 <define name="DISCORD_ALLOWED" />가 무조건 켜져 있어서
       html5에도 hxdiscord_rpc가 딸려 들어감.
  3) You cannot access the sys package while targeting js (for sys.io.File)
     → 채보 에디터(FileDialogHandler.hx)가 sys.io.File.getContent를 조건
       없이 사용. (참고: 같은 파일이 이미 #elseif (js && html5) 브랜치로
       브라우저 파일 선택창까지는 구현해놨지만, 정작 그 뒤에 파일 "내용"을
       읽는 부분만 데스크톱 전용 sys.io.File로 남아있었음 — 즉 업스트림
       엔진 자체가 html5 채보 에디터 로드 기능은 원래도 불완전했음.
       채보 에디터는 게임 플레이에는 쓰이지 않는 개발자 도구이므로, html5
       에서는 이 기능만 조용히 비활성화하고 나머지는 그대로 빌드되게 한다.)

해결 (2단계):
  A) sys.thread.*, sys.io.File, sys.FileSystem 등 조건 없는 import를
     #if !html5 로 감싸고, html5일 때는 동일한 이름의 메서드를 제공하는
     대체 클래스(backend.__html5compat.*)로 치환한다. 스레드 계열은 동기
     실행으로, 파일 계열은 "브라우저에는 없는 기능이니 조용히 무시"로
     동작해 컴파일도 되고 런타임에도 죽지 않게 만든다.
  B) Project.xml에서 <haxelib name="LIB" if="COND"/> 로 게이트된 라이브러리
     중, 실제 설치된 소스(haxelib path LIB로 조회)에 플랫폼 제한 #error가
     있고 COND가 if=/unless= 없이 완전히 무조건 정의된 define이면, 원본
     <define name="COND" /> 태그에 직접 unless="html5" 를 추가한다.
     (Psych Engine 자신이 HSCRIPT_ALLOWED 등을 if="desktop"으로 스코프하는
     것과 동일한, 이미 검증된 패턴.)

사용법:
  python3 patch_html5_threads.py <Project.xml 경로>
"""
import os
import re
import subprocess
import sys

COMPAT_PACKAGE = "backend.__html5compat"
COMPAT_FOLDER = "__html5compat"

# ── A) 대응 가능한 sys.* 클래스와, html5용 대체 구현 ──────────────────────
# key: (패키지 경로 "sys.thread" 등, 클래스명) → 대체 haxe 소스
COMPAT_CLASSES = {
    ("sys.thread", "FixedThreadPool"): f'''package {COMPAT_PACKAGE};

/**
 * html5(JS)는 실제 OS 스레드를 지원하지 않는다. sys.thread.FixedThreadPool을
 * 그대로 쓰면 컴파일 자체가 막히므로(This class is not available on this
 * target), 동일한 API(run/shutdown)를 제공하는 동기 실행 버전으로 대체한다.
 * 넘겨받은 함수를 즉시 실행한다 — 단일 스레드 환경에서 결과적으로 동일하게
 * 동작하지만 병렬성은 없다(웹에서는 원래 불가능한 부분).
 */
class FixedThreadPool {{
\tpublic function new(numThreads:Int = 1) {{}}
\tpublic function run(work:Void->Void):Void {{
\t\tif (work != null) work();
\t}}
\tpublic function shutdown():Void {{}}
}}
''',
    ("sys.thread", "ElasticThreadPool"): f'''package {COMPAT_PACKAGE};

/** FixedThreadPool과 동일한 이유로 대체된 동기 실행 버전. */
class ElasticThreadPool {{
\tpublic function new(min:Int = 1, max:Int = 1) {{}}
\tpublic function run(work:Void->Void):Void {{
\t\tif (work != null) work();
\t}}
\tpublic function shutdown():Void {{}}
}}
''',
    ("sys.thread", "Mutex"): f'''package {COMPAT_PACKAGE};

/** html5는 단일 스레드이므로 실제 잠금이 필요 없는 no-op Mutex 대체. */
class Mutex {{
\tpublic function new() {{}}
\tpublic function acquire():Void {{}}
\tpublic function tryAcquire():Bool {{ return true; }}
\tpublic function release():Void {{}}
}}
''',
    ("sys.thread", "Lock"): f'''package {COMPAT_PACKAGE};

/** html5는 단일 스레드이므로 항상 즉시 통과하는 no-op Lock 대체. */
class Lock {{
\tpublic function new() {{}}
\tpublic function wait(?timeoutMs:Float):Bool {{ return true; }}
\tpublic function release():Void {{}}
}}
''',
    ("sys.thread", "Thread"): f'''package {COMPAT_PACKAGE};

/** html5는 실제 스레드가 없으므로 호출한 자리에서 즉시 실행하는 대체. */
class Thread {{
\tpublic static function create(job:Void->Void):Thread {{
\t\tif (job != null) job();
\t\treturn new Thread();
\t}}
\tpublic static function current():Thread {{ return new Thread(); }}
\tpublic function new() {{}}
}}
''',
    ("sys.thread", "Deque"): f'''package {COMPAT_PACKAGE};

/** html5는 단일 스레드이므로 단순 배열 기반의 동기 Deque 대체. */
class Deque<T> {{
\tvar items:Array<T> = [];
\tpublic function new() {{}}
\tpublic function add(i:T):Void {{ items.push(i); }}
\tpublic function push(i:T):Void {{ items.unshift(i); }}
\tpublic function pop(block:Bool):Null<T> {{
\t\treturn items.length > 0 ? items.shift() : null;
\t}}
}}
''',
    ("sys.thread", "Tls"): f'''package {COMPAT_PACKAGE};

/** html5는 단일 스레드이므로 단순 값 저장소로 대체된 Tls. */
class Tls<T> {{
\tvar v:T;
\tpublic function new() {{}}
\tpublic var value(get, set):T;
\tfunction get_value():T return v;
\tfunction set_value(x:T):T {{ v = x; return v; }}
}}
''',
    ("sys.io", "File"): f'''package {COMPAT_PACKAGE};

/**
 * html5(JS)는 로컬 파일시스템에 직접 접근할 수 없다. sys.io.File을 그대로
 * 쓰면 컴파일 자체가 막히므로, 조용히 아무 동작도 하지 않는 대체 클래스로
 * 치환한다 — 채보 에디터처럼 데스크톱 전용 보조 기능만 웹에서 비활성화
 * 되고, 나머지 게임 로직은 정상적으로 빌드/실행된다.
 */
class File {{
\tpublic static function getContent(path:String):String {{
\t\ttrace('[html5] File.getContent 은 브라우저에서 지원되지 않습니다: ' + path);
\t\treturn '';
\t}}
\tpublic static function saveContent(path:String, content:String):Void {{
\t\ttrace('[html5] File.saveContent 은 브라우저에서 지원되지 않습니다: ' + path);
\t}}
\tpublic static function getBytes(path:String):haxe.io.Bytes {{
\t\treturn haxe.io.Bytes.alloc(0);
\t}}
}}
''',
    ("sys", "FileSystem"): f'''package {COMPAT_PACKAGE};

/** html5는 로컬 파일시스템이 없으므로 항상 "없음"으로 응답하는 대체. */
class FileSystem {{
\tpublic static function exists(path:String):Bool {{ return false; }}
\tpublic static function isDirectory(path:String):Bool {{ return false; }}
\tpublic static function readDirectory(path:String):Array<String> {{ return []; }}
\tpublic static function createDirectory(path:String):Void {{}}
\tpublic static function deleteFile(path:String):Void {{}}
\tpublic static function rename(path:String, newPath:String):Void {{}}
}}
''',
}

# import 문에서 잡아낼 패턴: sys.thread.X / sys.io.X / sys.FileSystem
IMPORT_RE = re.compile(
    r'^([ \t]*)import\s+(sys\.thread\.\w+|sys\.io\.\w+|sys\.FileSystem)\s*;[ \t]*$',
    re.MULTILINE
)


def _lookup_compat(qualified):
    """'sys.thread.FixedThreadPool' -> (('sys.thread','FixedThreadPool'), 'FixedThreadPool')"""
    parts = qualified.split('.')
    cls = parts[-1]
    pkg = '.'.join(parts[:-1])
    return (pkg, cls), cls


# [버그 수정] "You cannot access the sys package while targeting js" 는
# import 문 없이도 난다 — Paths.hx처럼 import 없이 그냥
# "sys.FileSystem.exists(...)" 를 코드 안에서 직접 완전경로로 쓰는
# 경우(Haxe는 이런 완전경로 인라인 참조를 허용함)는 IMPORT_RE(import
# 문만 잡는 정규식)로는 절대 못 잡는다. 이런 인라인 참조를 전부 찾아서
# "#if !html5 sys.FileSystem #else backend.__html5compat.FileSystem #end"
# 형태로 감싼다 — 뒤에 붙는 .exists(...) 같은 메서드 호출부는 두 분기가
# 동일한 API를 제공하므로 그대로 공통으로 남겨도 된다.
QUALIFIED_INLINE_RE = re.compile(
    r'\b(sys\.thread\.(?:FixedThreadPool|ElasticThreadPool|Mutex|Lock|Thread|Deque|Tls)'
    r'|sys\.io\.File'
    r'|sys\.FileSystem)\b(?!\s*;?\s*$)'
)


def patch_inline_qualified_refs(path, used_classes):
    """import 문이 아니라 코드 중간에서 완전경로(sys.FileSystem.exists(...) 등)로
    직접 쓰는 자리를 찾아 #if !html5 ... #else ... #end 로 감싼다."""
    with open(path, encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    changed = False
    out_lines = []
    for line in lines:
        stripped = line.strip()
        # import 문 자체는 별도 로직(patch_hx_file)이 처리하므로 건너뜀.
        # 이미 패치된 줄(#if !html5 재삽입 방지)도 건너뜀.
        if stripped.startswith('import ') or '#if !html5' in line or '__html5compat' in line:
            out_lines.append(line)
            continue

        def _replace(m):
            qualified = m.group(1)
            key, cls = _lookup_compat(qualified)
            if key not in COMPAT_CLASSES:
                print(f'[WARN] 인라인 참조 {qualified} 는 아직 html5 대체 클래스가 없음 ({path})')
                return m.group(0)
            used_classes.add(key)
            nonlocal changed
            changed = True
            return f'(#if !html5 {qualified} #else {COMPAT_PACKAGE}.{cls} #end)'

        new_line = QUALIFIED_INLINE_RE.sub(_replace, line)
        out_lines.append(new_line)

    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)
    return changed


def patch_hx_file(path, used_classes):
    # 1) 인라인 완전경로 참조 (import 없이 sys.FileSystem.exists(...) 처럼 쓰는 경우) 먼저 패치
    inline_changed = patch_inline_qualified_refs(path, used_classes)

    with open(path, encoding='utf-8', errors='ignore') as f:
        content = f.read()

    if not IMPORT_RE.search(content):
        return inline_changed

    changed = False

    def _replace(m):
        indent, qualified = m.group(1), m.group(2)
        key, cls = _lookup_compat(qualified)
        if key not in COMPAT_CLASSES:
            print(f'[WARN] {qualified} 는 아직 html5 대체 클래스가 없음 ({path})')
            return m.group(0)
        used_classes.add(key)
        nonlocal changed
        changed = True
        return (
            f'{indent}#if !html5\n'
            f'{indent}import {qualified};\n'
            f'{indent}#else\n'
            f'{indent}import {COMPAT_PACKAGE}.{cls};\n'
            f'{indent}#end'
        )

    new_content = IMPORT_RE.sub(_replace, content)
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    return changed or inline_changed


def patch_sys_apis(project_dir):
    """
    두 가지 일을 한다:
    1) 개별 파일에 "명시적으로" 있는 sys.thread.*/sys.io.*/sys.FileSystem
       import문을 #if !html5 로 감싸서 desktop/html5 분기시킨다.
       (예: FileDialogHandler.hx가 실제로 'import sys.io.File;' 을 갖고
       있는 경우 — 이건 그대로 두면 html5에서 'cannot access sys package'
       컴파일 에러가 난다.)
    2) [중요] compat 클래스(File/FileSystem/FixedThreadPool/...)는 위의
       "명시적 import를 찾은 경우"에만 만들면 안 된다 — 실제로는 대부분의
       파일(Mods.hx, ChartingState.hx, Paths.hx 등)이 명시적 import 없이
       source/import.hx의 전역 와일드카드(import sys.*; import sys.io.*;)
       를 통해서만 FileSystem/File 을 쓰고 있었다. 그래서 "명시적으로 찾은
       것만" 생성하면 FileSystem.hx 자체가 안 만들어져서 'Type not found:
       FileSystem' 에러가 났다. → compat 클래스는 항상 전체(COMPAT_CLASSES
       전부)를 만들어서, import.hx의 와일드카드(#elseif html5 import
       backend.__html5compat.*;)가 무엇을 요구하든 다 커버되게 한다.
    """
    used_classes = set()
    patched_files = []

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != f'__html5_compat_src']
        for fn in files:
            if fn.endswith('.hx'):
                fpath = os.path.join(root, fn)
                try:
                    if patch_hx_file(fpath, used_classes):
                        patched_files.append(fpath)
                except Exception as e:
                    print(f'[WARN] {fpath} 패치 중 오류(건너뜀): {e}')

    if patched_files:
        print(f'[sys.* 명시적 import] 파일별 #if !html5 분기 패치: {len(patched_files)}개')
        for f in patched_files:
            print(f'  - {f}')
    else:
        print('[sys.* 명시적 import] 개별 파일에 조건 없는 import문은 없음 (import.hx 와일드카드만 사용 중)')

    # compat 클래스는 항상 전체 생성 — import.hx 와일드카드가 뭘 요구할지
    # 모르므로 부분 생성하면 위와 같은 'Type not found' 재발 위험이 있음.
    compat_root = os.path.join(project_dir, '__html5_compat_src',
                                *COMPAT_PACKAGE.split('.'))
    os.makedirs(compat_root, exist_ok=True)
    for key, src in COMPAT_CLASSES.items():
        _pkg, cls = key
        with open(os.path.join(compat_root, f'{cls}.hx'), 'w', encoding='utf-8') as f:
            f.write(src)
    print(f'[compat] 클래스 {len(COMPAT_CLASSES)}개 전부 생성 완료: {compat_root} '
          f'({sorted(cls for _, cls in COMPAT_CLASSES.keys())})')
    return True  # __html5_compat_src source path 항상 등록 필요


# ── B) "데스크톱 전용 기능인데 define이 무조건 켜져있는" 패턴 범용 감지 ──────
DEFINE_TAG_RE = re.compile(r'<define\s+name="([^"]+)"([^>]*)/>')
HAXELIB_RE = re.compile(r'<haxelib\s+name="([^"]+)"([^>]*)/>')
IF_ATTR_RE = re.compile(r'\bif="([^"]+)"')
UNLESS_ATTR_RE = re.compile(r'\bunless="([^"]+)"')

PLATFORM_ERROR_HINTS = (
    'not available on this target',
    'target platform',
    'only supports',
    'supports only',
    'not supported on',
    'is not supported',
)


def haxelib_source_dir(lib_name):
    try:
        out = subprocess.run(
            ['haxelib', 'path', lib_name],
            capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('-D'):
            continue
        if os.path.isdir(line):
            return line
    return None


def dir_has_platform_error(src_dir):
    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            if not fn.endswith('.hx'):
                continue
            try:
                with open(os.path.join(root, fn), encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception:
                continue
            if '#error' not in content:
                continue
            low = content.lower()
            if any(hint in low for hint in PLATFORM_ERROR_HINTS):
                return True
    return False


def find_risky_defines(xml_content):
    unconditional_defines = set()
    for m in DEFINE_TAG_RE.finditer(xml_content):
        name, attrs = m.group(1), m.group(2)
        if not IF_ATTR_RE.search(attrs) and not UNLESS_ATTR_RE.search(attrs):
            unconditional_defines.add(name)

    if not unconditional_defines:
        return set()

    risky = set()
    for m in HAXELIB_RE.finditer(xml_content):
        lib_name, attrs = m.group(1), m.group(2)
        if_m = IF_ATTR_RE.search(attrs)
        if not if_m:
            continue
        cond_expr = if_m.group(1)
        cond_name = cond_expr.split()[0] if cond_expr.split() else cond_expr
        if cond_name not in unconditional_defines:
            continue
        src_dir = haxelib_source_dir(lib_name)
        if not src_dir:
            print(f'[WARN] haxelib "{lib_name}" 소스 경로를 찾지 못함 (건너뜀)')
            continue
        if dir_has_platform_error(src_dir):
            print(f'[감지] "{lib_name}" 라이브러리는 플랫폼 제한이 있는데, '
                  f'게이트 define "{cond_name}"이 무조건 켜져 있음 → html5에서 강제 해제')
            risky.add(cond_name)

    return risky


def apply_unless_html5(xml_content, define_name):
    def _sub(m):
        name, attrs = m.group(1), m.group(2)
        if name != define_name:
            return m.group(0)
        if 'unless=' in attrs or 'if=' in attrs:
            return m.group(0)
        return f'<define name="{name}"{attrs} unless="html5"/>'

    return DEFINE_TAG_RE.sub(_sub, xml_content, count=0)


# ── C) source/import.hx 패치 (진짜 근본 원인) ─────────────────────────────
# Psych Engine은 source/import.hx 에서
#   #if sys
#   import sys.*;
#   import sys.io.*;
#   #elseif js
#   import js.html.*;
#   #end
# 를 통해 "모든 .hx 파일에 자동으로" File/FileSystem 등을 주입한다.
# 문제는 html5(js)에서 import js.html.*; 가 켜지는데, 브라우저 DOM에는
# 우리가 원하는 것과 이름만 같은 완전히 다른 클래스들이 있다는 것:
#   - js.html.File / js.html.FileSystem : getContent/exists 같은 메서드가
#     전혀 없는 DOM 전용 클래스라서 "has no field X" 에러가 프로젝트 전체
#     (Paths.hx, Mods.hx, ChartingState.hx, ... 15개 이상 파일)에서 발생.
#   - js.html.Option : 브라우저 <select> 옵션 생성자로 new Option(text,
#     value, defaultSelected:Bool, selected) 시그니처를 가지는데, 이게
#     Psych 자신의 options.Option 클래스(같은 패키지라 원래는 암묵적으로
#     보여야 함)를 가려버려서 'String should be Bool' 에러가 남.
# 해결: html5일 때는 js.html.* 와일드카드를 우리 compat 패키지로 바꿔서
# 이 충돌들을 원천 차단한다. (sys/기타 js 타겟은 기존 동작 그대로 유지)
IMPORT_HX_OLD_BLOCK = re.compile(
    r'#if\s+sys\s*\n'
    r'import\s+sys\.\*;\s*\n'
    r'import\s+sys\.io\.\*;\s*\n'
    r'#elseif\s+js\s*\n'
    r'import\s+js\.html\.\*;\s*\n'
    r'#end'
)


def patch_import_hx(project_dir):
    for root, _dirs, files in os.walk(project_dir):
        if 'import.hx' not in files:
            continue
        path = os.path.join(root, 'import.hx')
        with open(path, encoding='utf-8', errors='ignore') as f:
            content = f.read()

        if 'backend.__html5compat' in content:
            print(f'[import.hx] 이미 패치되어 있음 — 건너뜀: {path}')
            continue

        m = IMPORT_HX_OLD_BLOCK.search(content)
        if not m:
            continue

        replacement = (
            '#if sys\n'
            'import sys.*;\n'
            'import sys.io.*;\n'
            '#elseif html5\n'
            f'import {COMPAT_PACKAGE}.*;\n'
            'import haxe.io.Path;\n'
            '#elseif js\n'
            'import js.html.*;\n'
            '#end'
        )
        new_content = content[:m.start()] + replacement + content[m.end():]
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f'[import.hx] 패치 완료 (html5용 js.html.* 와일드카드를 compat 패키지로 교체): {path}')
        return True
    return False


# ── D) 개별 sys 전용 API 호출 지점 패치 (Sys.sleep/getCwd, cpp.vm.Gc, FlxG.error) ─
# Sys.* 클래스는 js(html5) 타겟에는 존재하지 않는다(시스템 플랫폼
# 전용). 메서드마다 반환형이 달라서 html5 대체값도 다르게 줘야 한다
# (Void면 null도 되지만, Float/String/Int를 non-nullable 변수에 대입하는
# 코드가 있으면 null은 컴파일 에러가 남 -> 타입에 맞는 기본값 사용).
SYS_METHOD_FALLBACKS = {
    'sleep': 'null',
    'println': 'null',
    'print': 'null',
    'setCwd': 'null',
    'putEnv': 'null',
    'exit': 'null',
    'command': '0',
    'getCwd': '""',
    'programPath': '""',
    'time': '0.0',
    'cpuTime': '0.0',
    'environment': 'new Map()',
    'args': '[]',
}
SYS_CALL_RE = re.compile(r'Sys\.(\w+)\(([^()]*)\)')
CPP_GC_RE = re.compile(r'cpp\.vm\.Gc\.memInfo64\(cpp\.vm\.Gc\.MEM_INFO_USAGE\)')
FLXG_ERROR_RE = re.compile(r'FlxG\.error\(')


def patch_misc_sys_calls(project_dir):
    """
    Sys.* 계열(sleep/getCwd/time/println/...), cpp.vm.Gc, FlxG.error 를
    인라인 표현식으로 감싼다(줄 전체가 아니라 호출 부분만 - 'else
    Sys.sleep(0.001);' 처럼 다른 코드와 같은 줄에 있는 경우가 많아서
    줄 단위 앵커링은 놓치는 경우가 있었음).
    """
    patched = []
    unknown_methods = set()

    def _sys_replace(m):
        method, args = m.group(1), m.group(2)
        fallback = SYS_METHOD_FALLBACKS.get(method)
        if fallback is None:
            unknown_methods.add(method)
            fallback = 'null'  # 모르는 메서드는 일단 null로 안전하게 처리
        return f'(#if !html5 Sys.{method}({args}) #else {fallback} #end)'

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != '__html5_compat_src']
        for fn in files:
            if not fn.endswith('.hx'):
                continue
            fpath = os.path.join(root, fn)
            try:
                with open(fpath, encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception:
                continue

            original = content

            content = SYS_CALL_RE.sub(_sys_replace, content)
            content = CPP_GC_RE.sub('(#if cpp cpp.vm.Gc.memInfo64(cpp.vm.Gc.MEM_INFO_USAGE) #else 0.0 #end)', content)
            content = FLXG_ERROR_RE.sub('trace(', content)

            if content != original:
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(content)
                patched.append(fpath)

    if patched:
        print(f'[개별 API] Sys.*, cpp.vm.Gc, FlxG.error 패치된 파일 {len(patched)}개')
        for f in patched:
            print(f'  - {f}')
    if unknown_methods:
        print(f'[WARN] 반환형을 모르는 Sys.* 메서드 발견 (null로 대체함, 타입 에러 나면 SYS_METHOD_FALLBACKS에 추가 필요): {sorted(unknown_methods)}')
    return len(patched) > 0


def main():
    if len(sys.argv) < 2:
        print('사용법: patch_html5_threads.py <Project.xml 경로>')
        sys.exit(1)

    project_xml = sys.argv[1]
    project_dir = os.path.dirname(os.path.abspath(project_xml))

    needs_compat_source_path = patch_sys_apis(project_dir)
    patch_import_hx(project_dir)
    patch_misc_sys_calls(project_dir)

    with open(project_xml, encoding='utf-8') as f:
        xml_content = f.read()

    risky_defines = find_risky_defines(xml_content)
    if '<define name="MULTITHREADED_LOADING"' in xml_content:
        risky_defines.add('MULTITHREADED_LOADING')

    changed_defines = []
    for name in sorted(risky_defines):
        before = xml_content
        xml_content = apply_unless_html5(xml_content, name)
        if xml_content != before:
            changed_defines.append(name)

    source_path_added = False
    if needs_compat_source_path and '__html5_compat_src' not in xml_content:
        if '</project>' in xml_content:
            xml_content = xml_content.replace(
                '</project>',
                '\t<!-- [서버 자동 패치] sys.* html5 대체 클래스 경로 -->\n'
                '\t<source path="__html5_compat_src" if="html5" />\n</project>'
            )
            source_path_added = True

    if not changed_defines and not source_path_added:
        print('추가 패치 불필요')
        return

    with open(project_xml, 'w', encoding='utf-8') as f:
        f.write(xml_content)

    print(f'Project.xml 패치 완료: {project_xml}')
    if changed_defines:
        print(f'  html5에서 비활성화된 define: {changed_defines}')
    if source_path_added:
        print('  __html5_compat_src 소스 경로 등록됨')


if __name__ == '__main__':
    main()
