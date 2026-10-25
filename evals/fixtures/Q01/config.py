def resolve(cli, environment, file_value, default=20):
    for candidate in (cli, environment, file_value, default):
        if candidate is not None:
            return candidate
