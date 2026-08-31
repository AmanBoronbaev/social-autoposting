import os

os.environ.setdefault("APP_JWT_SECRET", "unit-test-secret-must-be-at-least-thirty-two-bytes")
os.environ.setdefault("APP_ENCRYPTION_KEY", "p_-wsztTDdPwCLWXjWWAag0Mww6_z2WTpXgh2bMjBsE=")
os.environ.setdefault("APP_DATABASE_URL", "sqlite:////tmp/autoposting_platform_test.db")
