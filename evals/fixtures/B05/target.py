from pathlib import Path
def read_text(path):
    return Path(str(path).split()[0]).read_text(encoding='utf-8')
