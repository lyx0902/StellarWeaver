def parse_size(value, default=20):
    try:
        return min(int(value), 100)
    except (ValueError, TypeError):
        return default
