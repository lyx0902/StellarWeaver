import argparse
def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    print(' '.join(map(str, range(10))))
if __name__ == '__main__':
    main()
