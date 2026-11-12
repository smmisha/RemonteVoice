# Варианты подключения RemonteVoice

Текущий транспорт — закреплённый TLS WebSocket к выбранному приватному IPv4. Код Android не привязывает сокет к Wi-Fi: другой локальный IP-канал может использовать тот же протокол. Это архитектурная совместимость, а не подтверждённая проверка всех моделей и адаптеров.

| Канал | Что требуется | Состояние |
| --- | --- | --- |
| Wi-Fi через роутер | Одна локальная сеть, адрес ноутбука в QR | На Android ранее проверены сопряжение и звук Windows; устойчивость микрофона требует повторной проверки |
| USB-модем Android | Подключить кабель, включить USB-модем в настройках Android, выбрать адрес USB-интерфейса в Host, получить новый QR | Приоритет следующей физической проверки; ADB и root для передачи по IP не нужны |
| Точка доступа ноутбука или телефона | Включить системную точку доступа и подключить второе устройство | Не требует домашнего роутера; использует Wi-Fi; физическая проверка ожидается |
| Ethernet | Поддерживаемый телефоном USB Ethernet адаптер и общая локальная сеть | Зависит от телефона/адаптера; физическая проверка ожидается |
| Bluetooth PAN | Системная Bluetooth-сеть и новый сжатый аудиорежим | Требуется разработка кодека/согласования и проверка; текущий PCM не подходит |
| Bluetooth RFCOMM | Отдельный транспорт, защищённое сопряжение и сжатый звук | Исследован, не реализован |
| Wi-Fi Direct | Поддерживаемые адаптеры, отдельное обнаружение и сопряжение | Возможное дополнительное развитие, не реализовано |
| ADB reverse через USB | Режим разработчика, доверие ПК и отдельный явный debug transport | Возможен для разработки; текущая проверка Android не допускает loopback, ослаблять её для LAN не следует |

## Почему Bluetooth требует отдельной работы

48 000 отсчётов × 4 байта × (1 канал микрофона + 2 канала воспроизведения) — 4,608 Мбит/с до накладных расходов TLS/TCP. Bluetooth Classic EDR имеет номинальный предел PHY 3 Мбит/с; полезная скорость ниже. Поэтому нужен сжатый формат, например Opus, с согласованием возможностей. Передача экрана в первом Bluetooth-режиме планируется выключенной.

Для iPhone USB Personal Hotspot проверяется отдельно; на Windows Apple указывает необходимость Apple Devices либо iTunes. Возможности Android RFCOMM не означают аналогичную доступность на iOS. Передача через интернет/VPN пока не входит в согласованный локальный режим.

## Первичные источники, проверены 22 октября 2026

- [Android: USB, Bluetooth и Wi-Fi tethering](https://support.google.com/android/answer/9059108).
- [AOSP: USB, Wi-Fi, Bluetooth и Ethernet tethering](https://source.android.com/docs/core/ota/modular-system/tethering).
- [Windows: Bluetooth PAN](https://support.microsoft.com/en-au/windows/hardware/bluetooth/connect-to-a-bluetooth-network-in-windows).
- [Windows: Mobile hotspot](https://support.microsoft.com/en-gb/windows/experience/connectivity-networking/use-your-windows-device-as-a-mobile-hotspot).
- [Bluetooth SIG: скорости PHY](https://www.bluetooth.com/learn-about-bluetooth/tech-overview/).
- [Android RFCOMM](https://developer.android.com/develop/connectivity/bluetooth/connect-bluetooth-devices), [Windows RFCOMM](https://learn.microsoft.com/en-us/windows/win32/bluetooth/bluetooth-and-socket).
- [Android Wi-Fi Direct](https://developer.android.com/develop/connectivity/wifi/wifip2p).
- [iPhone: Personal Hotspot](https://support.apple.com/en-ie/111785).
