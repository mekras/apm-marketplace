# Реестр APM Mekras

Реестр содержит переносимые пакеты для агентов ИИ. Каждый пакет хранится в
`packages/` в составе, который допускается к установке в целевой проект.

## Подключение

```bash
apm marketplace add mekras/apm-marketplace --ref master
```

## Пакеты

### ai-russian-language

Навыки для применения русского языка в агентной разработке.

Установка для Codex:

```bash
apm install ai-russian-language@mekras --target codex
```

Установка для Claude:

```bash
apm install ai-russian-language@mekras --target claude
```
