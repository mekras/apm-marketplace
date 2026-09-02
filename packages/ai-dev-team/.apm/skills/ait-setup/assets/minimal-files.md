# Минимальные стартовые файлы

Этот файл содержит рабочие заготовки для `ait-setup`. Не копируй их механически:
замени плейсхолдеры на сведения из диалога или оставь открытый вопрос только
после явного разрешения человека.

## README.md

```markdown
# <Название проекта>

<Одно-два предложения: что это за проект и зачем он нужен пользователю.>

## Начало работы

<Первое практическое действие пользователя. Если команда неизвестна, не
выдумывай её.>

## Документация

- <Ссылка на подробный документ, если он уже есть.>
```

## AGENTS.md

```markdown
# Правила для агентов

Проект использует `ai-dev-team`.

Параметры настройки проекта для `ait-setup` хранятся в `.ai-dev-team/state.yml`.

Оставляй в этом файле только локальные рабочие правила, которые нужны агенту во
время обычных задач.

## Концепция проекта

Концепция находится в `<точный путь и при необходимости раздел>`.

Перед предметной работой учитывай концепцию. Перед передачей результата проверь,
что он не расходится с ней.

## Граф влияния проекта

Граф влияния находится в `.ai-dev-team/project-impact.json`.

После любого изменения сопоставь изменённые пути и выполни полный транзитивный
анализ через `ait-impact-analysis`. Не завершай задачу с несопоставленным путём
или незакрытой затронутой вершиной.

## Общие правила

- Если в проекте есть `CHANGELOG.md`, проверяй необходимость его обновления
  через `ait-changelog`.
- Если корпус знаний хранится не в `knowledge/`, укажи путь здесь явно.
- Соблюдай выбранную проектом схему сообщений коммитов.
- Перед изменением существующего файла сначала прочитай его текущее содержимое.
- Не записывай секреты, токены, пароли и приватные ключи в файлы проекта.
```

## .ai-dev-team/state.yml

```yaml
product: mekras/ai-dev-team
updated_at: 2026-06-27
setup:
  operating_context:
    kind: noncommercial
    note: volunteer-maintained
  project_profile:
    forms: [library]
    users: [software-developer]
    interfaces: [api]
    delivery: [dependency]
  visibility: public
  git:
    enabled: true
  commit_messages:
    scheme: conventional-commits
    # Для scheme: custom добавьте format и не менее двух examples.
    # custom:
    #   format: '<описание структуры сообщения>'
    #   examples:
    #     - '<первый пример>'
    #     - '<второй пример>'
  changelog:
    status: present
    path: CHANGELOG.md
  knowledge:
    status: present
    path: knowledge/
  impact_graph:
    status: present
    path: .ai-dev-team/project-impact.json
    schema_version: 1
```

Для проекта без журнала изменений или корпуса знаний используй
`status: declined`. Поле `applied_version` добавляй после завершённого
пересмотра проекта под конкретную версию `ai-dev-team`.

После явного решения о разработке на основе спецификаций добавь в `setup`
отдельный блок. Не включай его в профиль проекта:

```yaml
  sdd:
    status: adopted
    level: aligned
    scope: [product-behavior, external-contracts]
    specification_paths: [docs/requirements/**]
    derived_paths: [src/**, tests/**]
    approval: before-implementation
    exception_policy:
      path_pattern: .ai-dev-team/exceptions/<exception-id>.md
    review_condition: repeated-false-blocks-or-excessive-maintenance-cost
```

Используй фактические пути и область проекта. Для явного отказа достаточно
`status: declined`. Для временной остановки укажи `status: suspended`, причину и
условие возобновления.

## .ai-dev-team/project-impact.json

Используй `ait-impact-analysis/assets/project-impact-template.json` как
начальную структуру. Адаптируй вершины, пути, проверки и рёбра к фактическому
проекту, не копируй пример как готовую модель зависимостей.

## CHANGELOG.md

```markdown
# История изменений

Все заметные изменения этого проекта документируются в этом файле.

## [Невыпущено]
```

## .gitignore

```gitignore
.DS_Store
*.tmp
*.swp
.env
.env.*
```

## LICENSE

Для `LICENSE` используй только текст выбранной лицензии или явное решение
человека о закрытом правовом режиме. Не выбирай лицензию самостоятельно.
