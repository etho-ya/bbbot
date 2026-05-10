# Risk Engine Callback Guide

Risk Engine должен отправлять решение (approve/reject) обратно боту через HTTP POST запрос.

### 🌐 Endpoint
```http
POST https://midas-trade.mooo.com/api/re/callback
```

### 📝 Format (JSON Body)
```json
{
    "hash": "abc123def456",
    "decision": "approve", 
    "score": 0.85,
    "reason": "Trend confirmed, VaR OK",
    "secret": "123QWEasd"
}
```

### 📋 Fields Description
| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| **hash** | `string` | **Yes** | Уникальный хэш сигнала из сообщения в @uebot_report. |
| **decision** | `string` | **Yes** | `"approve"` (открыть сделку) или `"reject"` (отменить). |
| **score** | `float` | No | Множитель для размера позиции (0.1 - 1.0). Если `0.5`, бот откроет сделку на 50% объема. |
| **reason** | `string` | No | Причина решения для логов. |
| **secret** | `string` | **Yes** | Текущее значение: `123QWEasd` |

---
**Note:** Если `decision="approve"`, бот проверяет `score`. Если `score` не передан, используется 100% от настроенного риска.
