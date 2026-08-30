#!/usr/bin/env python3
"""
[버그 수정] html5(JS) 빌드에서 sys.thread.* 컴파일 에러 근본 해결.

증상:
  ERROR .../std/sys/thread/FixedThreadPool.hx:26: This class is not available on this target

원인:
  Psych Engine(및 파생 엔진)의 source/states/LoadingState.hx가
    import sys.thread.FixedThreadPool;
    import sys.thread.Mutex;
  를 #if 조건 없이 무조건 import하고, MULTITHREADED_LOADING define과
  무관하게 threadPool = new FixedThreadPool(...) 를 항상 생성한다.
  (MULTITHREADED_LOADING은 스레드 "개수"만 바꿀 뿐, 스레드풀 생성 자체는
  막지 못함). 즉 Project.xml의 define을 아무리 손봐도 소용이 없고,
  html5는 sys.thread 패키지 자체가 존재하지 않아 무조건 컴파일이 깨진다.

해결:
  업로드된 모드(mod_src) 전체에서 sys.thread.* 를 "조건 없이" import하는
  라인을 찾아 #if !html5 로 감싸고, html5일 때는 동일한 API(run/shutdown/
  acquire/release 등)를 제공하는 동기(synchronous) 대체 클래스를 쓰도록
  치환한다. 대체 클래스는 이 스크립트가 생성해서 Project.xml에
  <source path="__html5_compat_src" if="html5" /> 로 추가 클래스패스를
  등록한다. (JS는 원래 단일 스레드이므로 "즉시 동기 실행"으로 대체해도
  기능적으로는 동일하게 동작한다 — 병렬성만 없을 뿐.)

사용법:
  python3 patch_html5_threads.py <Project.xml 경로>
"""
import os
import re
import sys

# 대응 가능한 sys.thread.* 클래스와, html5용 대체 구현.
# (필요한 것만 감지해서 만든다 — 안 쓰는 클래스는 건드리지 않음)
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
            # 대응 클래스가 없으면 원본 그대로 두되, html5에서는 어차피
            # 실패할 것이므로 경고만 남긴다(빌드 로그에서 확인 가능).
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


def main():
    if len(sys.argv) < 2:
        print('사용법: patch_html5_threads.py <Project.xml 경로>')
        sys.exit(1)

    project_xml = sys.argv[1]
    project_dir = os.path.dirname(os.path.abspath(project_xml))

    used_classes = set()
    patched_files = []

    for root, dirs, files in os.walk(project_dir):
        # 우리가 만들 compat 소스 폴더 자체는 스캔 대상에서 제외
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
        return

    print(f'패치된 파일 {len(patched_files)}개, 대체 클래스: {sorted(used_classes)}')
    for f in patched_files:
        print(f'  - {f}')

    # compat 클래스 소스 생성
    compat_root = os.path.join(project_dir, '__html5_compat_src', 'backend', '__html5threadcompat')
    os.makedirs(compat_root, exist_ok=True)
    for cls in used_classes:
        with open(os.path.join(compat_root, f'{cls}.hx'), 'w', encoding='utf-8') as f:
            f.write(COMPAT_CLASSES[cls])
    print(f'compat 클래스 생성 완료: {compat_root}')

    # Project.xml에 소스 경로 + MULTITHREADED_LOADING 무효화 등록
    with open(project_xml, encoding='utf-8') as f:
        xml_content = f.read()

    injections = []
    if '__html5_compat_src' not in xml_content:
        injections.append('\t<source path="__html5_compat_src" if="html5" />')
    if '<undefine name="MULTITHREADED_LOADING"' not in xml_content:
        injections.append('\t<undefine name="MULTITHREADED_LOADING" if="html5" />')

    if injections and '</project>' in xml_content:
        block = (
            '\n\t<!-- [서버 자동 패치] html5는 스레드를 지원하지 않아 '
            'sys.thread.* 를 동기 대체 클래스로 치환 -->\n'
            + '\n'.join(injections) + '\n'
        )
        xml_content = xml_content.replace('</project>', block + '</project>')
        with open(project_xml, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        print(f'Project.xml 패치 완료: {project_xml}')
    elif not injections:
        print('Project.xml에 이미 패치가 적용되어 있음 — 건너뜀')
    else:
        print(f'[WARN] </project> 태그를 찾지 못해 Project.xml은 패치하지 못함: {project_xml}')


if __name__ == '__main__':
    main()
