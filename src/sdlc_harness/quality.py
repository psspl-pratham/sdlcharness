import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import QA
from .sandbox import Sandbox

DEPENDENCY_SCRIPT = '''import json, re, sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from packaging.requirements import InvalidRequirement, Requirement

errors = []
for number, raw in enumerate(Path('requirements.txt').read_text(encoding='utf-8-sig').splitlines(), 1):
    line = re.split(r'\\s+#', raw, maxsplit=1)[0].strip()
    if not line or line.startswith('#'):
        continue
    prefix = f'requirements.txt:{number}: '
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        errors.append(prefix + 'use a package name and optional version constraints; pip options and file references are unsupported in this POC')
        continue
    if requirement.marker and not requirement.marker.evaluate():
        continue
    if requirement.url or requirement.extras:
        errors.append(prefix + f"'{requirement.name}' uses a URL or extras; only installed named packages are supported in the POC sandbox")
        continue
    try:
        installed = version(requirement.name)
    except PackageNotFoundError:
        hint = " Use 'pydantic>=2,<3'; pydantic.v1 is an import namespace." if requirement.name.lower() in {'pydanticv1', 'pydantic.v1'} else ''
        errors.append(prefix + f"'{requirement.name}' is not installed in the POC sandbox." + hint)
        continue
    if requirement.specifier and not requirement.specifier.contains(installed, prereleases=True):
        errors.append(prefix + f"'{requirement.name}{requirement.specifier}' does not match tested sandbox version {installed}")
print(json.dumps({'errors': errors}))
sys.exit(bool(errors))
'''

COMPILE_SCRIPT = '''import pathlib, py_compile, sys
errors = []
for path in sorted(pathlib.Path('.').rglob('*.py')):
    if '.git' not in path.parts:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as error:
            errors.append(str(error))
print('\\n'.join(errors))
sys.exit(bool(errors))
'''


class QualityEngine:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    def check(self, workspace: Path) -> QA:
        # Early build tasks may not have created the manifest yet. When present,
        # check it inside the same sandbox as the tests, without pip or network access.
        if (workspace / "requirements.txt").exists():
            dependencies = self.sandbox.run(workspace, ["-c", DEPENDENCY_SCRIPT])
            if dependencies.returncode:
                try:
                    errors = json.loads(dependencies.output)["errors"]
                except (ValueError, KeyError, TypeError):
                    errors = [dependencies.output or "Dependency validation process failed"]
                errors = errors or ["Dependency validation process failed"]
                return QA(errors=len(errors), dependency_errors=errors,
                          output="Dependency validation failed:\n" + "\n".join(errors))
        compile_result = self.sandbox.run(workspace, ["-c", COMPILE_SCRIPT])
        if compile_result.returncode:
            return QA(compile_errors=[compile_result.output or "Compile process failed"], output=compile_result.output)
        results = workspace / ".harness"
        results.mkdir(exist_ok=True)
        report = results / "junit.xml"
        report.unlink(missing_ok=True)
        test_result = self.sandbox.run(workspace, ["-m", "pytest", "-q", "--junitxml=.harness/junit.xml"])
        qa = QA(output=test_result.output)
        if not report.exists():
            qa.errors = 1
            qa.output += "\nTest runner did not produce JUnit XML"
            return qa
        try:
            root = ET.parse(report).getroot()
            cases = list(root.iter("testcase"))
            qa.failed = sum(c.find("failure") is not None for c in cases)
            qa.errors = sum(c.find("error") is not None for c in cases)
            qa.skipped = sum(c.find("skipped") is not None for c in cases)
            for case in cases:
                problem = case.find("failure")
                if problem is None:
                    problem = case.find("error")
                if problem is not None:
                    qa.failures.append({
                        "test": f"{case.get('classname', '')}::{case.get('name', '')}",
                        "message": problem.get("message", "")[:1000],
                        "traceback": (problem.text or "")[-2500:],
                    })
            qa.passed = len(cases) - qa.failed - qa.errors - qa.skipped
            suite_errors = sum(int(s.get("errors", 0)) for s in root.iter("testsuite"))
            qa.errors = max(qa.errors, suite_errors)
        except (ET.ParseError, ValueError) as error:
            qa.errors = 1
            qa.output += f"\nInvalid JUnit report: {error}"
        qa.green = (test_result.returncode == 0 and qa.passed > 0 and not qa.failed and not qa.errors)
        if not qa.green and not qa.failed and not qa.errors:
            qa.errors = 1
            qa.output += "\nNo passing tests or test runner exited unsuccessfully"
        return qa
