"""Single static syntax batch; the parent once runner owns the attempt ledger."""
import ast
from pathlib import Path
import subprocess
from jinja2 import Environment

root=Path(__file__).resolve().parents[1]
files=set(subprocess.check_output(['git','diff','--name-only'],cwd=root,text=True).splitlines())
files.update(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=root,text=True).splitlines())
failures=[]
for name in sorted(files):
    p=root/name
    try:
        if p.suffix=='.py':ast.parse(p.read_text(encoding='utf-8'),filename=name)
        elif p.suffix=='.html':Environment().parse(p.read_text(encoding='utf-8'))
        elif p.suffix in {'.js','.cjs'}:subprocess.run(['node','--check',str(p)],check=True,capture_output=True,text=True)
    except Exception as exc:failures.append((name,str(exc)))
diff = subprocess.run(['git','diff','--check'],cwd=root,capture_output=True,text=True)
if diff.returncode: failures.append(('whitespace',diff.stdout))
print({'files':len(files),'failures':failures})
raise SystemExit(bool(failures))
