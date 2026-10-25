import sys
from business import InputError, parse
def main(value):
    try: return parse(value), 0
    except InputError as error:
        print(str(error), file=sys.stderr)
        return None, 2
