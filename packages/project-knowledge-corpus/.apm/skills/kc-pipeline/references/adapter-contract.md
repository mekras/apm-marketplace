# Исполняемый договор адаптеров

Договор связывает поле `adapter` из `source.yml` с проектными командами. Код
конкретного носителя остаётся в проекте-потребителе. Коллекция поставляет
переносимый договор, исполнитель и проверки границ.

## Версия 1

Определение без `contract_version` считается адаптером версии 1:

```yaml
adapters:
  builtin.local-file:
    argv: [python3, tools/adapter-local-file.py, --source-id, "{source_id}", --source-dir, "{source_dir}", --locator, "{locator}"]
    working_directory: .
    write_paths: [knowledge/data]
```

Такой адаптер поддерживает только получение. Результат сохраняет прежний
формат с `contract_version: 1` и статусами `synced`, `partial`, `changed`,
`unchanged`, `new`, `removed`, `manual-required`, `access-limited`,
`fetch-error`, `unsupported-adapter` или `invalid-registry`.

## Версия 2

Адаптер версии 2 объявляет отдельные операции:

```yaml
adapters:
  project.restricted-source:
    contract_version: 2
    operations:
      probe:
        argv: [python3, tools/restricted-source-adapter.py, probe, --source-id, "{source_id}", --profile, "{profile_name}"]
        working_directory: .
        write_paths: []
      fetch:
        argv: [python3, tools/restricted-source-adapter.py, fetch, --source-id, "{source_id}", --source-dir, "{source_dir}", --locator, "{locator}", --profile, "{profile_name}"]
        working_directory: .
        write_paths: [knowledge/data]
      verify:
        argv: [python3, tools/restricted-source-adapter.py, verify, --source-id, "{source_id}", --source-dir, "{source_dir}", --locator, "{locator}", --profile, "{profile_name}"]
        working_directory: .
        write_paths: [knowledge/data]
      authorize:
        argv: [python3, tools/restricted-source-adapter.py, authorize, --profile, "{profile_name}"]
        working_directory: .
        write_paths: []
```

- `probe` проверяет готовность и авторизацию без изменения корпуса;
- `fetch` получает конкретный снимок;
- `verify` сопоставляет сохранённый снимок с источником;
- `authorize` выполняет необязательную интерактивную настройку и никогда не
  запускается конвейером автоматически.

Перед `fetch` и `verify` исполнитель сам запускает `probe`. Целевая операция
начинается только после статуса `ready`. Явный запуск:

```bash
python3 .apm/skills/kc-pipeline/scripts/run-corpus-operations.py \
  knowledge --operations knowledge/operations.yml --run-adapters \
  --adapter-operation verify --source SOURCE-ID
```

Для интерактивной настройки пользователь должен явно выбрать
`--adapter-operation authorize`.

## Локальные профили

Карточка источника может хранить только вид авторизации, необходимые
возможности, логическое `profile_name` и допустимость интерактивной настройки.
Текущая готовность, секрет и данные сессии принадлежат игнорируемому локальному
слою проекта, например `.local/access-profiles.yml` или защищённому хранилищу.
Адаптер сам разрешает логическое имя в локальные учётные данные. Секрет нельзя
передавать через `argv`, стандартный вывод, отчёт или Git.

`probe` различает статусы `ready`, `profile-missing`,
`interactive-login-required`, `permission-denied`, `terms-decision-required`,
`technical-unavailable` и `unsupported-adapter`. Это локальное наблюдение. Оно
не записывается в переносимый `verification.yml` и не меняет прежнюю успешную
проверку снимка.

## Результат версии 2

Команда с нулевым кодом завершения выводит один JSON-объект:

```json
{
  "contract_version": 2,
  "operation": "probe",
  "source_id": "SOURCE-ID",
  "adapter": "project.restricted-source",
  "status": "ready",
  "message": "Локальный профиль готов к чтению.",
  "artifacts": []
}
```

Для `fetch` используются статусы версии 1. Для `verify` допустимы `verified`,
`partially-verified`, `unverified`, `mismatch`, `access-limited` и
`fetch-error`. Успешная `verify` записывает `verification.yml` по договору
`kc-inventory`. Для `authorize` допустимы `ready`,
`interactive-login-required`, `permission-denied` и `technical-unavailable`.

`source_id`, `adapter` и `operation` должны совпадать с вызовом. `artifacts`
перечисляет репо-относительные созданные или проверенные пути, но не расширяет
`write_paths`.

## Границы исполнения

`argv` передаётся без оболочки. Доступны только `{source_id}`, `{source_dir}`,
`{locator}` и `{profile_name}`. `working_directory` и `write_paths` задаются
относительно корня проекта. `probe` не может менять Git или корпус. После
команды исполнитель проверяет изменения относительно `write_paths`.

Неуспешная операция не может менять `verification.yml`. Неизвестная версия
договора отклоняется. Неизвестный адаптер получает `unsupported-adapter`.
`adapter: manual` без регистрации не считается успешной синхронизацией.

Не храните в настройках токены, cookies, пароли, ключи API, заголовки
авторизации и закрытые адреса. Исполнитель отклоняет явные поля секретов,
обезличивает похожие значения в сообщении об ошибке и не выводит их в отчёт.

`builtin.local-file` может проверить локальный файл и обновить его паспорт, не
публикуя сам файл. Проектный индексный адаптер может получить только список
единиц. Частные сервисы не требуют специальных значений общего договора.
