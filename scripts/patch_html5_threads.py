#!/usr/bin/env python3
"""
[버그 수정] html5(JS) 빌드 컴파일 에러 근본 해결 — 스레드 API + "데스크톱 전용
기능인데 define이 무조건 켜져있는" 패턴 둘 다 처리.

배경 (겪은 순서대로):
  1) ERROR sys/thread/FixedThreadPool.hx: This class is not available on this target
     → Psych Engine의 LoadingState.hx/Discord.hx가 sys.thread.FixedThreadPool/
       Mutex/Thread를 #if 조건 없이 무조건 import+사용. MULTITHREADED_LOADING
       define은 스레드 "개수"만 바꿀 뿐 스레드풀 생성 자체를 막지 못해서
       define을 손보는 것만으론 해결이 안 됐음.
  2) ERROR hxdiscord_rpc/Discord.hx: 'Discord RPC supports only C++ target platforms.'
     → 원인이 완전히 같은 패턴. Project.xml의 <define name="DISCORD_ALLOWED" />
       가 if="desktop" 같은 조건 없이 무조건 켜져 있어서, html5 빌드에도
       hxdiscord_rpc 라이브러리가 딸려 들어가고, 그 라이브러리 자체가 C++
       타겟이 아니면 #error로 즉시 컴파일을 막아버림.

  → 이런 "데스크톱/네이티브 전용 기능인데 무조건 켜진 define" 패턴이 모드마다
    다른 이름으로 계속 나올 수 있으므로, 하나씩 발견할 때마다 고치는 대신
    범용적으로 자동 감지하도록 만든다.

해결 (2단계):
  A) sys.thread.* 무조건 import → #if !html5 로 감싸고 동기 실행 대체
     클래스(backend.__html5threadcompat.*)로 치환. (검증됨: 실제 빌드에서
     FixedThreadPool 에러가 사라지고 다음 에러로 넘어간 것으로 확인)
  B) Project.xml에서 <haxelib name="LIB" if="COND"/> 형태로 "COND일 때만
     설치되는" 라이브러리를 찾고, 그 라이브러리가 실제 설치된 소스
     (haxelib path LIB로 조회)에 플랫폼 제한 #error가 있는지 검사한다.
     #error가 있고, COND가 Project.xml에서 if=/unless= 없이 완전히
     무조건 정의된 define이면 → 그 define을 정의하는 원본
     <define name="COND" /> 태그에 직접 unless="html5" 를 추가한다.
     (Psych Engine 자신이 HSCRIPT_ALLOWED 등을 if="desktop"으로 올바르게
     스코프하는 것과 완전히 동일한, 이미 검증된 패턴이라 가장 안전하다.)

사용법:
  python3 patch_html5_threads.py <Project.xml 경로>
"""
import os
import re
import subprocess
import sys

# ── A) sys.thread.* 대응 가능한 클래스와, html5용 동기 대체 구현 ──────────
COMPAT_CLASSES = {
    "FixedThreadPool": '''package backend.__html5threadcompat;

/**
 * html5(JS)는 실제 OS 스레드를 지원하지 않는다. sys.thread.FixedThreadPool을
 * 그대로 쓰면 컴파일 자체가 막히므로(This class is not available on this
 * target), 동일한 API(run/shutdown)를 제공하는 동기 실행 버전으로 대체한다.
 * 넘겨받은 함수를 즉시 실행한다 — 단일 스레드 환경에서 결과적으로 동일하게
 * 동작하지만 병렬성은 없다(웹에서는 원래 불가능한 부분).
 */
class FixedThreadPool {
\tpublic function new(numThreads:Int = 1) {}
\tpublic function run(work:Void->Void):Void {
\t\tif (work != null) work();
\t}
\tpublic function shutdown():Void {}
}
''',
    "ElasticThreadPool": '''package backend.__html5threadcompat;

/** FixedThreadPool과 동일한 이유로 대체된 동기 실행 버전. */
class ElasticThreadPool {
\tpublic function new(min:Int = 1, max:Int = 1) {}
\tpublic function run(work:Void->Void):Void {
\t\tif (work != null) work();
\t}
\tpublic function shutdown():Void {}
}
''',
    "Mutex": '''package backend.__html5threadcompat;

/** html5는 단일 스레드이므로 실제 잠금이 필요 없는 no-op Mutex 대체. */
class Mutex {
\tpublic function new() {}
\tpublic function acquire():Void {}
\tpublic function tryAcquire():Bool { return true; }
\tpublic function release():Void {}
}
''',
    "Lock": '''package backend.__html5threadcompat;

/** html5는 단일 스레드이므로 항상 즉시 통과하는 no-op Lock 대체. */
class Lock {
\tpublic function new() {}
\tpublic function wait(?timeoutMs:Float):Bool { return true; }
\tpublic function release():Void {}
}
''',
    "Thread": '''package backend.__html5threadcompat;

/** html5는 실제 스레드가 없으므로 호출한 자리에서 즉시 실행하는 대체. */
class Thread {
\tpublic static function create(job:Void->Void):Thread {
\t\tif (job != null) job();
\t\treturn new Thread();
\t}
\tpublic static function current():Thread { return new Thread(); }
\tpublic function new() {}
}
''',
    "Deque": '''package backend.__html5threadcompat;

/** html5는 단일 스레드이므로 단순 배열 기반의 동기 Deque 대체. */
class Deque<T> {
\tvar items:Array<T> = [];
\tpublic function new() {}
\tpublic function add(i:T):Void { items.push(i); }
\tpublic function push(i:T):Void { items.unshift(i); }
\tpublic function pop(block:Bool):Null<T> {
\t\treturn items.length > 0 ? items.shift() : null;
\t}
}
''',
    "Tls": '''package backend.__html5threadcompat;

/** html5는 단일 스레드이므로 단순 값 저장소로 대체된 Tls. */
class Tls<T> {
\tvar v:T;
\tpublic function new() {}
\tpublic var value(get, set):T;
\tfunction get_value():T return v;
\tfunction set_value(x:T):T { v = x; return v; }
}
''',
}

IMPORT_RE = re.compile(r'^([ \t]*)import\s+sys\.thread\.(\w+)\s*;[ \t]*$', re.MULTILINE)


def patch_hx_file(path, used_classes):
    with open(path, encoding='utf-8', errors='ignore') as f:
        content = f.read()

    matches = list(IMPORT_RE.finditer(content))
    if not matches:
        return False

    changed = False

    def _replace(m):
        indent, cls = m.group(1), m.group(2)
        if cls not in COMPAT_CLASSES:
            print(f'[WARN] sys.thread.{cls} 는 아직 html5 대체 클래스가 없음 ({path})')
            return m.group(0)
        used_classes.add(cls)
        nonlocal changed
        changed = True
        return (
            f'{indent}#if !html5\n'
            f'{indent}import sys.thread.{cls};\n'
            f'{indent}#else\n'
            f'{indent}import backend.__html5threadcompat.{cls};\n'
            f'{indent}#end'
        )

    new_content = IMPORT_RE.sub(_replace, content)
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    return changed


def patch_sys_thread(project_dir):
    used_classes = set()
    patched_files = []

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != '__html5_compat_src']
        for fn in files:
            if fn.endswith('.hx'):
                fpath = os.path.join(root, fn)
                try:
                    if patch_hx_file(fpath, used_classes):
                        patched_files.append(fpath)
                except Exception as e:
                    print(f'[WARN] {fpath} 패치 중 오류(건너뜀): {e}')

    if not used_classes:
        print('sys.thread.* 무조건 import 패턴 없음 — 패치 불필요')
        return False

    print(f'[sys.thread] 패치된 파일 {len(patched_files)}개, 대체 클래스: {sorted(used_classes)}')
    for f in patched_files:
        print(f'  - {f}')

    compat_root = os.path.join(project_dir, '__html5_compat_src', 'backend', '__html5threadcompat')
    os.makedirs(compat_root, exist_ok=True)
    for cls in used_classes:
        with open(os.path.join(compat_root, f'{cls}.hx'), 'w', encoding='utf-8') as f:
            f.write(COMPAT_CLASSES[cls])
    print(f'[sys.thread] compat 클래스 생성 완료: {compat_root}')
    return True  # __html5_compat_src source path 등록 필요


# ── B) "데스크톱 전용 기능인데 define이 무조건 켜져있는" 패턴 범용 감지 ──────
DEFINE_TAG_RE = re.compile(r'<define\s+name="([^"]+)"([^>]*)/>')
HAXELIB_RE = re.compile(r'<haxelib\s+name="([^"]+)"([^>]*)/>')
IF_ATTR_RE = re.compile(r'\bif="([^"]+)"')
UNLESS_ATTR_RE = re.compile(r'\bunless="([^"]+)"')

# 라이브러리 소스에서 플랫폼 제한을 알리는 전형적인 문구들
PLATFORM_ERROR_HINTS = (
    'not available on this target',
    'target platform',
    'only supports',
    'supports only',
    'not supported on',
    'is not supported',
)


def haxelib_source_dir(lib_name):
    """`haxelib path LIB` 결과에서 실제 소스 경로를 추출."""
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
    """
    if=/unless= 없이 무조건 정의된 define이면서, 그 define으로 게이트된
    haxelib가 실제로는 플랫폼 제한(#error)이 있는 경우의 define 이름 목록.
    """
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
    """
    <define name="X" /> 원본 태그를 <define name="X" unless="html5" /> 로
    직접 치환 (Psych Engine 자신이 다른 데스크톱 전용 define을 스코프하는
    것과 동일한, 이미 검증된 패턴).
    """
    def _sub(m):
        name, attrs = m.group(1), m.group(2)
        if name != define_name:
            return m.group(0)
        if 'unless=' in attrs or 'if=' in attrs:
            return m.group(0)  # 이미 조건이 있으면 건드리지 않음
        return f'<define name="{name}"{attrs} unless="html5"/>'

    return DEFINE_TAG_RE.sub(_sub, xml_content, count=0)


def main():
    if len(sys.argv) < 2:
        print('사용법: patch_html5_threads.py <Project.xml 경로>')
        sys.exit(1)

    project_xml = sys.argv[1]
    project_dir = os.path.dirname(os.path.abspath(project_xml))

    # A) sys.thread.* 치환
    needs_compat_source_path = patch_sys_thread(project_dir)

    # B) 위험한 무조건 define 탐지 + 원본 define 태그 직접 수정
    with open(project_xml, encoding='utf-8') as f:
        xml_content = f.read()

    risky_defines = find_risky_defines(xml_content)
    # MULTITHREADED_LOADING은 라이브러리 게이트가 아니라 엔진 자체 코드 문제라
    # 위 B 탐지로는 안 잡힘. 이미 A에서 컴파일 에러 자체는 해결됐지만, 굳이
    # 스레드풀을 만들 필요도 없으므로 성능상 같이 꺼둔다.
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
                '\t<!-- [서버 자동 패치] sys.thread.* html5 동기 대체 클래스 경로 -->\n'
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
