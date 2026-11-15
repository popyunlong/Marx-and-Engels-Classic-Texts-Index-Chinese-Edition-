"""Single syntax pass of this task's changed Python/Jinja/JavaScript sources."""
import ast
from pathlib import Path
import subprocess
from jinja2 import Environment

root=Path(__file__).resolve().parents[1]
files=set(subprocess.check_output(['git','diff','--name-only'],cwd=root,text=True,encoding='utf-8').splitlines())
files.update(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=root,text=True,encoding='utf-8').splitlines())
checked=[]
for name in sorted(files):
    path=root/name
    if path.suffix=='.py':ast.parse(path.read_text('utf-8'),filename=name)
    elif path.suffix=='.html':Environment().parse(path.read_text('utf-8'))
    elif path.suffix in {'.js','.cjs'}:subprocess.run(['node','--check',str(path)],check=True)
    else:continue
    checked.append(name)
subprocess.run(['git','diff','--check'],cwd=root,check=True)
print('Syntax passed:',len(checked),'files')
print('\n'.join(checked))
