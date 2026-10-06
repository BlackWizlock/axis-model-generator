# Локальная проверка и доставка

Первая поставка проверяет принятое Python-ядро. Образ имеет назначение
`check`: он не содержит готовый веб-сервис и не запускается на VPS.
Незавершённые исходники авторизации и выбранная позже база не входят в него.

## Требования

Нужны Git, Python 3.14 или новее для управляющего скрипта, локальный Docker
Engine с buildx и поддержкой Linux amd64. Сборка на Apple Silicon использует
эмуляцию amd64. Официальный Python 3.14.5 slim закреплён digest в
`deploy/dev/Dockerfile.check`; версия Python на машине разработчика может
отличаться. Git внутри образа нужен для проверки автономного экспорта.

Все сборки и приёмочные проверки выполняются локально в Docker. Облачные
runners, платный CI и registry не используются. Загрузка официального
базового образа и пакета Git происходит при локальной сборке.

## Команды

Из корня репозитория с сохранёнными в Git изменениями:

```sh
python3 scripts/dev-pipeline.py check --revision HEAD --output _output/check-001
python3 scripts/dev-pipeline.py build --revision HEAD --output _output/build-001
python3 scripts/dev-pipeline.py inspect-isolation --policy deploy/dev/pipeline-policy.example.json
```

Каждый output должен быть новым игнорируемым Git каталогом этого репозитория.
Исходники берутся из точного публичного inventory выбранного коммита,
проходят проверки экспортёра и копируются в отдельный Docker context.
Незакоммиченные файлы, история Git, ключи и модели туда не попадают.
Дополнительный аргумент `--private-policy` у check/build передаёт локальную
приватную политику экспортёру. Содержимое политики не записывается в образ.

`check` собирает образ и запускает весь экспортируемый unittest suite.
`build` выполняет тот же шаг, затем сохраняет `image.tar.gz` и `manifest.json`.
Тесты работают от UID/GID 10001, без сети, с read-only rootfs, tmpfs в `/tmp`,
без capabilities, с no-new-privileges и ограничениями CPU, памяти и процессов.
Они проверяют также автономный экспорт и повторный экспорт без родительского
репозитория. Логи остаются в output; `check-result.json` содержит код выхода.

Тег `axis-model-generator/check:<полный SHA>` не заменяет существующий образ
с другим ID. Манифест schema 1 связывает SHA исходников, платформу Linux amd64,
ID образа, digest Dockerfile и размер/SHA-256 архива. Это запись результата,
не криптографическая подпись доверенного релиза.

Доставка требует явно выбранного SSH target с предварительно проверенным
host key и разрешением на Docker у SSH пользователя:

```sh
python3 scripts/dev-pipeline.py ship --manifest _output/build-001/manifest.json --ssh-target deploy@server.example.invalid
```

Скрипт повторно проверяет локальный образ именно по ID, сверяет metadata
архива с этим ID и передаёт распакованный `docker save` на stdin `docker load`.
После загрузки он сверяет удалённые ID, архитектуру и labels. Сервер образ
не пересобирает. Скрипт не запускает контейнер, не выполняет restart/prune,
не меняет compose/Caddy и не задаёт production deploy.

Архив проверяется целиком до SSH: только каноничные пути без traversal,
повторов и ссылок; OCI layout/index и вся цепочка descriptors должны
содержать единственный собственный тег и единственную платформу Linux amd64.
Лишние ссылки, platforms и неиспользуемые OCI blobs вызывают отказ. Сборка
отключает attestations/SBOM в buildx, чтобы check archive содержал одну
платформу; это не заменяет будущую лицензионную проверку server runtime.

Docker с containerd image store может показывать OCI index ID, а старый
image store показывает config ID. Архив проверяется по соответствующей
цепочке metadata. Для доставки нужны совместимые image stores с одинаковым
представлением ID; несовпадение удалённого ID вызывает отказ даже при
совпадении исходников. Автоматического обхода этой проверки нет.

## Изоляция

Указать каждый проверяемый контейнер явно:

```sh
python3 scripts/dev-pipeline.py inspect-isolation --ssh-target deploy@server.example.invalid --container model-generator-api --policy deploy/dev/pipeline-policy.example.json
```

Inspect читает выбранные структурные поля без Env. Контейнеру нужны
`com.axis.model-generator.project=axis-model-generator`, числовой nonroot UID,
cap_drop ALL, no-new-privileges, собственные сети и volumes с тем же label.
Policy принимает только точные имена `model-generator_*` без wildcard.
Чужие Axis сети/volumes, host network/ports, bind mounts, Docker socket,
host PID/IPC и устройства вызывают отказ. Пример policy является списком
разрешённых имён и не создаёт инфраструктуру.

Volume inspect дополнительно читает Driver и Options. Допускается только
встроенный `local` driver без options: bind/NFS и сторонние plugins вызывают
отказ даже при собственном имени и label. Локальная сборка привязана к
явному Docker context `default`, локальному unix socket или named pipe
текущей Windows-машины и единственному узлу buildx с endpoint `default`.
Удалённые Docker contexts, named pipes и builder aliases отклоняются.

Без контейнеров или при точном ответе Docker «No such container» результат
`not_deployed`. Ошибки SSH, прав или недоступный daemon не считаются отсутствием. `structural_pass` подтверждает
только структуру Docker. DNS/connectivity probes к платящим тенантам ещё
не выполнены и имеют `not_verified`; доступ к их данным не требуется.
Общий Caddy должен проверяться отдельно как gateway и не даёт API права
подключаться к клиентским сетям.

## Условия запуска с данными

Backend аккаунтов и очереди ожидает выбора. Бакет не является диском SQL.
Модели, отчёты и backups требуют отдельного private YC bucket, отдельного
service account и минимальных прав. Общие ключи/бакет Axis Platform и
разделение только prefix недопустимы. Секреты передаются при runtime и не
входят в build arguments, layers, frontend или логи. Платные YC ресурсы
этими командами не создаются.

Регион Timeweb VPS не подтверждён inventory. IP/hostname не доказывают
локализацию. До запуска с пользовательскими данными нужны реальные resource
metadata VPS, bucket, БД и backups, проверки IAM и сетевого доступа.
`locality` и `storage_iam` сейчас `not_verified`. Доставка check image без
данных не закрывает эти условия и не является запуском веб-продукта.
