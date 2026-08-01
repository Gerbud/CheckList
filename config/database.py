from django.core.exceptions import ImproperlyConfigured


MYSQL_REQUIRED_VARIABLES = (
    'MYSQL_DATABASE',
    'MYSQL_USER',
    'MYSQL_PASSWORD',
    'MYSQL_HOST',
    'MYSQL_PORT',
)


def build_database_config(base_dir, environ):
    engine = environ.get('DATABASE_ENGINE', 'sqlite').strip().lower()
    if engine == 'sqlite':
        return {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': base_dir / 'db.sqlite3',
            }
        }
    if engine != 'mysql':
        raise ImproperlyConfigured(
            'DATABASE_ENGINE должен быть sqlite или mysql.'
        )

    missing = [
        name
        for name in MYSQL_REQUIRED_VARIABLES
        if not environ.get(name, '').strip()
    ]
    if missing:
        raise ImproperlyConfigured(
            'Для DATABASE_ENGINE=mysql задайте переменные: '
            + ', '.join(missing)
        )

    default = {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': environ['MYSQL_DATABASE'],
        'USER': environ['MYSQL_USER'],
        'PASSWORD': environ['MYSQL_PASSWORD'],
        'HOST': environ['MYSQL_HOST'],
        'PORT': environ['MYSQL_PORT'],
        'CONN_MAX_AGE': 60,
        # Shared-hosting MySQL may close an idle connection before Django's
        # persistent-connection lifetime expires. Ping once per request so a
        # dropped connection is replaced before the first real query.
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
    test_database = environ.get('MYSQL_TEST_DATABASE', '').strip()
    if test_database:
        default['TEST'] = {'NAME': test_database}
    return {'default': default}
