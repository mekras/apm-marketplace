# Исполняемый договор адаптеров

Этот договор связывает поле `adapter` из `source.yml` с проектной командой, но
не включает код получения конкретного носителя в коллекцию. Он позволяет
подключать новый источник добавлением карточки и определения адаптера в
настройки операций.

## Реестр

В файле, передаваемом через `--operations`, добавьте `adapters`. Ключ должен
совпадать с `adapter` в карточке источника.

```yaml
adapters:
  builtin.local-file:
    argv: [python3, tools/adapter-local-file.py, --source-id, "{source_id}", --source-dir, "{source_dir}", --locator, "{locator}"]
    working_directory: .
    write_paths: [knowledge/data]
  project.web-index:
    argv: [python3, tools/adapter-web-index.py, --source-id, "{source_id}", --locator, "{locator}"]
    working_directory: .
    write_paths: [knowledge/data]
```

`argv` передаётся без оболочки. Доступны только подстановки `{source_id}`,
`{source_dir}` и `{locator}`. `working_directory` и `write_paths` задаются
относительно корня проекта. Средство запуска проверяет через Git, что адаптер
не изменил файлы вне `write_paths`.

Не записывайте в отслеживаемые настройки токены, cookies, пароли, ключи API,
заголовки авторизации или закрытые адреса. Средство отклоняет явные поля
секретов и URL с `?token=`; остальные секреты должны поступать из локального
неотслеживаемого слоя, среды процесса или защищённого хранилища проекта.

## Запуск

Обычное планирование не запускает адаптеры. Выполнение требует явного флага:

```bash
python3 .apm/skills/kc-pipeline/scripts/run-corpus-operations.py \
  knowledge --operations knowledge/operations.yml --run-adapters
```

Чтобы ограничить запуск одним или несколькими источниками, повторяйте
`--source SOURCE-ID`. Неизвестный идентификатор источника — ошибка запуска.
Неизвестное имя адаптера не подменяется похожим способом: оно попадает в отчёт
со статусом `unsupported-adapter`.

## Результат

Адаптер с нулевым кодом завершения обязан вывести в стандартный вывод ровно
один JSON-объект:

```json
{
  "contract_version": 1,
  "source_id": "SOURCE-ID",
  "adapter": "project.web-index",
  "status": "unchanged",
  "message": "В индекс добавлены только метаданные; полного содержимого не сохранено.",
  "artifacts": ["knowledge/data/example-source/items.yml"]
}
```

`source_id` и `adapter` должны совпадать с карточкой источника. `message`
объясняет результат следующему запуску. `artifacts` содержит репо-относительные
пути созданных или проверенных артефактов; это список для отчёта, а не разрешение
на запись вне `write_paths`.

Допустимые статусы: `synced`, `partial`, `changed`, `unchanged`, `new`,
`removed`, `manual-required`, `access-limited`, `fetch-error`,
`unsupported-adapter`, `invalid-registry`. Ненулевой код команды превращается в
`fetch-error`; при этом средство сохраняет текст ошибки в локальном отчёте.

## Примеры границ

`builtin.local-file` может проверить наличие локального файла, обновить его
паспорт и вернуть `unchanged` или `changed`. Сам файл остаётся в локальном
слое, если политика копирования не разрешает его публикацию.

`project.web-index` может получить sitemap или индекс страниц и записать только
метаданные в `items.yml`. Sitemap — необязательный механизм обнаружения, а не
отдельный вид источника. Адаптер не сохраняет полное содержимое страницы, пока
стратегия хранения и политика копирования этого не разрешают.
