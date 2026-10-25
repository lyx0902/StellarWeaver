def run(action):
    try:
        action()
    except Exception:
        return 0
    return 0
