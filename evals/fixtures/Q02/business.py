class InputError(ValueError): pass
def parse(value):
    try: return int(value)
    except ValueError: raise InputError('invalid integer') from None
