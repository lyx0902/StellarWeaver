from pathlib import Path
_cache = {}
def cached_text(path):
    key = str(path)
    if key not in _cache:
        _cache[key] = Path(path).read_text(encoding='utf-8')
    return _cache[key]
