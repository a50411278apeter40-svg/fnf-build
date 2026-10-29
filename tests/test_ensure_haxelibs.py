"""Project.xml 이 요구하는 haxelib 를 빌드 전에 빠짐없이 설치하고, 실제로 설치됐는지 검증한다.

배경: 실행 35414419153 이 'Could not find haxelib "flixel-ui"' 로 실패했다. 모드의 Project.xml 은 flixel-ui 를 선언했는데
설치 단계가 haxelib 를 하나도 설치하지 못했고(로그: '이미 설치된 라이브러리:' 빈 값) 아무 경고도 없이 빌드로 넘어갔다."""
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'scripts', 'ensure_haxelibs.py')
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import ensure_haxelibs as eh

# 실제 실패한 모드(VsDaveArchived)가 선언했던 8줄 (실행 로그에서 그대로 옮김)
REAL_MOD = """<?xml version="1.0" encoding="utf-8"?>
<project>
\t<haxelib name="flixel" />
\t<haxelib name="flixel-addons" />
\t<haxelib name="hscript" />
\t<haxelib name="actuate" />
\t<haxelib name="flixel-ui" />
\t<haxelib name="faxe" if='switch'/>
\t<haxelib name="hxCodec" if="desktop || android" />
\t<haxelib name="discord_rpc" if="desktop"/>
</project>"""


def names(libs):
    return [l['name'] for l in libs]


class FakeHaxelib:
    """haxelib 흉내: installed 에 있어야 'path' 가 성공하고, install 은 installable 에 있는 것만 성공해 installed 에 추가한다."""
    def __init__(self, calls, installed=(), installable=()):
        self.calls = calls
        self.installed = set(installed)
        self.installable = set(installable)

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        sub, name = cmd[1], (cmd[2] if len(cmd) > 2 else '')
        if sub == 'path':
            ok = name in self.installed
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, '' if ok else 'Error: Library %s is not installed' % name, '')
        if sub == 'install':
            if name in self.installable:
                self.installed.add(name)
                return subprocess.CompletedProcess(cmd, 0, 'ok', '')
            return subprocess.CompletedProcess(cmd, 1, 'Error: no such library', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')


class Extract(unittest.TestCase):
    def test_real_failing_mod_includes_flixel_ui(self):
        libs = eh.find_declared_libs(REAL_MOD)
        self.assertIn('flixel-ui', names(libs))
        self.assertEqual(len(libs), 8)

    def test_namespace_is_ignored(self):
        x = REAL_MOD.replace('<project>', '<project xmlns="http://lime.software/project/1.0.2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
        self.assertIn('flixel-ui', names(eh.find_declared_libs(x)))
        self.assertEqual(len(eh.find_declared_libs(x)), 8)

    def test_parser_rejects_file_but_we_still_find_libs(self):
        for bad in ('<!-- build -- release -->', '<set name="X" value="a & b" />', '<set name="X" value="a < b" />', '<app title="x">'):
            x = REAL_MOD.replace('<project>', '<project>\n' + bad)
            self.assertIn('flixel-ui', names(eh.find_declared_libs(x)), bad)

    def test_garbage_gives_empty_not_exception(self):
        for g in ('', '   ', 'not xml at all', '\x00\x01', None):
            self.assertEqual(eh.find_declared_libs(g), [], repr(g))

    def test_version_is_kept(self):
        libs = eh.find_declared_libs('<project><haxelib name="hxdiscord_rpc" version="1.2.4" /></project>')
        self.assertEqual(libs[0]['version'], '1.2.4')

    def test_no_version_is_empty_string(self):
        self.assertEqual(eh.find_declared_libs('<project><haxelib name="flixel"/></project>')[0]['version'], '')

    def test_duplicates_collapsed_keeping_the_version(self):
        libs = eh.find_declared_libs('<project><haxelib name="flixel"/><haxelib name="flixel" version="5.6.1"/></project>')
        self.assertEqual(names(libs), ['flixel'])
        self.assertEqual(libs[0]['version'], '5.6.1')

    def test_haxelib_inside_a_comment_is_not_counted(self):
        x = '<project><!-- <haxelib name="ghost"/> --><haxelib name="flixel"/></project>'
        self.assertEqual(names(eh.find_declared_libs(x)), ['flixel'])

    def test_haxelib_inside_a_comment_is_not_counted_even_when_parser_fails(self):
        x = '<project>\n<!-- a -- b -->\n<!-- <haxelib name="ghost"/> -->\n<haxelib name="flixel"/></project>'
        self.assertEqual(names(eh.find_declared_libs(x)), ['flixel'])

    def test_condition_attribute_is_kept(self):
        libs = {l['name']: l for l in eh.find_declared_libs(REAL_MOD)}
        self.assertEqual(libs['faxe']['if'], 'switch')
        self.assertEqual(libs['discord_rpc']['if'], 'desktop')
        self.assertEqual(libs['flixel-ui']['if'], '')


class Classification(unittest.TestCase):
    def test_unconditional_is_required(self):
        self.assertTrue(eh.is_required({'name': 'flixel-ui', 'if': '', 'unless': ''}))

    def test_desktop_only_is_not_required_for_html5(self):
        for cond in ('desktop', 'switch', 'android', 'cpp', 'windows', 'linux', 'mac', 'desktop || android', 'ios'):
            self.assertFalse(eh.is_required({'name': 'x', 'if': cond, 'unless': ''}), cond)

    def test_html5_conditions_are_required(self):
        for cond in ('html5', 'web', 'js', 'html5 || desktop', 'desktop || html5'):
            self.assertTrue(eh.is_required({'name': 'x', 'if': cond, 'unless': ''}), cond)

    def test_unless_html5_is_not_required(self):
        self.assertFalse(eh.is_required({'name': 'x', 'if': '', 'unless': 'html5'}))

    def test_unknown_condition_defaults_to_required(self):
        self.assertTrue(eh.is_required({'name': 'x', 'if': 'someCustomDefine', 'unless': ''}))

    def test_real_mod_required_set(self):
        req = [l['name'] for l in eh.find_declared_libs(REAL_MOD) if eh.is_required(l)]
        self.assertEqual(sorted(req), ['actuate', 'flixel', 'flixel-addons', 'flixel-ui', 'hscript'])


class NameSafety(unittest.TestCase):
    def test_safe_names(self):
        for n in ('flixel-ui', 'hxCodec', 'discord_rpc', 'hscript-iris', 'grig.audio', 'linc_luajit', 'hxcpp-debug-server'):
            self.assertTrue(eh.valid_name(n), n)

    def test_unsafe_names_rejected(self):
        for n in ('', ' ', '--global', '-x', 'a b', 'a;id', '$(id)', '`id`', 'a|b', 'a&b', '../x', 'a/b', 'a\nb', 'x' * 200, '.hidden', 'a\\b'):
            self.assertFalse(eh.valid_name(n), repr(n))

    def test_unsafe_versions_rejected(self):
        for v in ('--global', '1.0; id', '$(x)', '1 2', 'a/b', '-1'):
            self.assertFalse(eh.valid_version(v), repr(v))
        for v in ('', '1.2.4', '5.6.1', '4.0.0-beta.1', 'git', '1,2,3'):
            self.assertTrue(eh.valid_version(v), repr(v))

    def test_malicious_declared_name_is_never_passed_to_haxelib(self):
        calls = []
        f = FakeHaxelib(calls, installable={'flixel'})
        x = '<project><haxelib name="--global"/><haxelib name="a;id"/><haxelib name="flixel"/></project>'
        eh.ensure(eh.find_declared_libs(x), runner=f.run)
        passed = [c[2] for c in calls if len(c) > 2]
        self.assertEqual(set(passed), {'flixel'}, calls)

    def test_malicious_version_is_never_passed_to_haxelib(self):
        calls = []
        f = FakeHaxelib(calls, installable={'flixel'})
        eh.ensure(eh.find_declared_libs('<project><haxelib name="flixel" version="--global"/></project>'), runner=f.run)
        flat = [a for c in calls for a in c]
        self.assertNotIn('--global', flat)


class Ensure(unittest.TestCase):
    def libs(self, x=REAL_MOD):
        return eh.find_declared_libs(x)

    def test_installs_what_is_missing_and_reports_success(self):
        calls = []
        f = FakeHaxelib(calls, installed={'flixel'}, installable={'flixel-addons', 'hscript', 'actuate', 'flixel-ui', 'faxe', 'hxCodec', 'discord_rpc'})
        res = eh.ensure(self.libs(), runner=f.run)
        self.assertEqual(res['missing_required'], [])
        self.assertIn(['haxelib', 'install', 'flixel-ui', '--quiet'], calls)

    def test_already_installed_is_not_reinstalled(self):
        calls = []
        f = FakeHaxelib(calls, installed={'flixel', 'flixel-addons', 'hscript', 'actuate', 'flixel-ui'})
        eh.ensure(self.libs(), runner=f.run)
        self.assertFalse([c for c in calls if c[1] == 'install' and c[2] in ('flixel', 'flixel-ui')])

    def test_required_lib_that_cannot_be_installed_is_reported(self):
        calls = []
        f = FakeHaxelib(calls, installed={'flixel', 'flixel-addons', 'hscript', 'actuate'}, installable=set())
        res = eh.ensure(self.libs(), runner=f.run)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_optional_missing_lib_is_only_a_warning(self):
        calls = []
        f = FakeHaxelib(calls, installed={'flixel', 'flixel-addons', 'hscript', 'actuate', 'flixel-ui'}, installable=set())
        res = eh.ensure(self.libs(), runner=f.run)
        self.assertEqual(res['missing_required'], [])
        self.assertIn('faxe', res['missing_optional'])

    def test_install_that_claims_success_but_lib_still_missing_is_caught(self):
        """설치 명령이 성공 코드를 내도 실제로 안 깔렸으면 잡아야 한다(이번 사고의 핵심: 설치했다고 믿고 넘어감)."""
        class Liar(FakeHaxelib):
            def run(self, cmd, **kw):
                self.calls.append(list(cmd))
                if cmd[1] == 'install':
                    return subprocess.CompletedProcess(cmd, 0, 'ok', '')
                return subprocess.CompletedProcess(cmd, 1, 'not installed', '')
        res = eh.ensure(self.libs('<project><haxelib name="flixel-ui"/></project>'), runner=Liar([]).run)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_path_command_exit_zero_with_not_installed_text_counts_as_missing(self):
        """haxelib path 가 종료 코드 0 인데 'not installed' 를 출력하는 경우도 없는 것으로 본다."""
        def odd(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "Library flixel-ui is not installed : run 'haxelib install flixel-ui'", '')
        res = eh.ensure(self.libs('<project><haxelib name="flixel-ui"/></project>'), runner=odd)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_install_error_does_not_raise(self):
        def boom(cmd, **kw):
            raise OSError('no haxelib binary')
        res = eh.ensure(self.libs('<project><haxelib name="flixel-ui"/></project>'), runner=boom)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_timeout_does_not_raise(self):
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        res = eh.ensure(self.libs('<project><haxelib name="flixel-ui"/></project>'), runner=slow)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_version_is_installed_and_pinned(self):
        calls = []
        f = FakeHaxelib(calls, installable={'hxdiscord_rpc'})
        eh.ensure(eh.find_declared_libs('<project><haxelib name="hxdiscord_rpc" version="1.2.4"/></project>'), runner=f.run)
        self.assertIn(['haxelib', 'install', 'hxdiscord_rpc', '1.2.4', '--quiet'], calls)
        self.assertIn(['haxelib', 'set', 'hxdiscord_rpc', '1.2.4'], calls)

    def test_empty_declared_list(self):
        self.assertEqual(eh.ensure([], runner=FakeHaxelib([]).run)['missing_required'], [])


class CoreLibs(unittest.TestCase):
    """선언이 하나도 안 잡혀도(파서 문제 등) 핵심 목록은 빌드 전에 검증된다."""
    def test_core_libs_are_always_checked(self):
        calls = []
        f = FakeHaxelib(calls, installable=set(eh.CORE_LIBS))
        res = eh.ensure_with_core([], runner=f.run)
        self.assertEqual(res['missing_required'], [])
        installs = [c[2] for c in calls if c[1] == 'install']
        for lib in ('flixel-ui', 'flixel', 'flixel-addons', 'hscript', 'actuate', 'tjson'):
            self.assertIn(lib, installs, lib)

    def test_core_includes_flixel_ui(self):
        self.assertIn('flixel-ui', eh.CORE_LIBS)

    def test_core_lib_failure_is_not_fatal(self):
        """핵심 기본값은 '있으면 좋은' 것이라 설치에 실패해도 빌드를 막지 않는다(모드가 선언하지 않았으면 필요 없을 수 있다)."""
        calls = []
        f = FakeHaxelib(calls, installable={'flixel'})
        res = eh.ensure_with_core(eh.find_declared_libs('<project><haxelib name="flixel"/></project>'), runner=f.run)
        self.assertEqual(res['missing_required'], [])

    def test_declared_lib_missing_stays_fatal_even_with_core(self):
        calls = []
        f = FakeHaxelib(calls, installable=set(eh.CORE_LIBS) - {'flixel-ui'})
        res = eh.ensure_with_core(eh.find_declared_libs(REAL_MOD), runner=f.run)
        self.assertEqual(res['missing_required'], ['flixel-ui'])

    def test_declared_version_wins_over_core_default(self):
        calls = []
        f = FakeHaxelib(calls, installable={'tjson'})
        eh.ensure_with_core(eh.find_declared_libs('<project><haxelib name="tjson" version="1.3.0"/></project>'), runner=f.run)
        self.assertIn(['haxelib', 'install', 'tjson', '1.3.0', '--quiet'], calls)
        self.assertNotIn(['haxelib', 'install', 'tjson', '1.4.0', '--quiet'], calls)


class CommandLine(unittest.TestCase):
    def run_cli(self, xml, already, installable):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, 'Project.xml')
            open(proj, 'w', encoding='utf-8').write(xml)
            fake = os.path.join(d, 'haxelib')
            open(fake, 'w').write(textwrap.dedent("""\
                #!/bin/bash
                STATE=%s/state
                case "$1" in
                  path) grep -qx "$2" "$STATE" && exit 0 || { echo "Error: Library $2 is not installed" >&2; exit 1; } ;;
                  install) case " %s " in *" $2 "*) echo "$2" >> "$STATE"; exit 0;; *) echo "Error: no such lib" >&2; exit 1;; esac ;;
                  *) exit 0 ;;
                esac
                """ % (d, installable)))
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            open(os.path.join(d, 'state'), 'w').write('\n'.join(already) + '\n')
            env = dict(os.environ, PATH=d + os.pathsep + os.environ['PATH'])
            return subprocess.run([sys.executable, SCRIPT, proj], capture_output=True, text=True, env=env, timeout=60)

    def test_exit_zero_when_everything_available(self):
        r = self.run_cli(REAL_MOD, [], ' '.join(['flixel', 'flixel-addons', 'hscript', 'actuate', 'flixel-ui', 'faxe', 'hxCodec', 'discord_rpc'] + eh.CORE_LIBS))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_exit_nonzero_and_names_the_missing_lib(self):
        r = self.run_cli(REAL_MOD, [], ' '.join(['flixel', 'flixel-addons', 'hscript', 'actuate'] + [l for l in eh.CORE_LIBS if l != 'flixel-ui']))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('flixel-ui', r.stdout + r.stderr)

    def test_missing_project_file_is_not_a_crash(self):
        r = subprocess.run([sys.executable, SCRIPT, '/no/such/Project.xml'], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn('Project.xml', r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
