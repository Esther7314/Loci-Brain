"""List every non-docstring STRING literal that differs between two versions of a file."""
import ast, io, subprocess, sys
sys.stdout.reconfigure(encoding='utf-8')
ref, path = sys.argv[1], sys.argv[2]

def strings(src):
    try: tree = ast.parse(src)
    except SyntaxError: return []
    doc = set()
    for n in ast.walk(tree):
        b = getattr(n, 'body', None)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and b:
            f = b[0]
            if isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant) and isinstance(f.value.value, str):
                doc.add((f.value.lineno, f.value.col_offset))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and (n.lineno, n.col_offset) not in doc:
            out.append(n.value)
    return out

old = subprocess.run(['git','show',f'{ref}:{path}'],capture_output=True).stdout.decode('utf-8','replace')
new = io.open(path, encoding='utf-8').read()
a, b = strings(old), strings(new)
sa, sb = set(a), set(b)
print(f'{path}：非 docstring 字符串 {len(a)} → {len(b)}')
gone, added = sorted(sa - sb), sorted(sb - sa)
print(f'\n没了的 {len(gone)} 条：')
for s in gone: print('   -', repr(s[:95]))
print(f'\n新增的 {len(added)} 条：')
for s in added: print('   +', repr(s[:95]))
