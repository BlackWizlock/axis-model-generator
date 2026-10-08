# Лицензии поставки

Собственный код Apache-2.0, LICENSE и NOTICE в корне. API/worker не содержат
Blender/bpy. NumPy, Pillow, FastAPI, Starlette, Uvicorn, Psycopg и boto3
сохраняют собственные лицензии; версии и хеши в requirements-web.lock.
Лицензии Python/Debian сохраняются в базовом образе.
Frontend уведомления в web/THIRD-PARTY-NOTICES.md и web/assets/fonts.
Полный inventory зависимостей и лицензионная сверка являются выпускным условием.

Backup image содержит PostgreSQL 16.15 clients и их shared libraries из
закреплённого официального Debian образа PostgreSQL. PostgreSQL сохраняет
собственную разрешительную лицензию; libpq, OpenSSL, Kerberos, LDAP и прочие
динамические зависимости должны войти в полный inventory перед публикацией.
