# Trading Bot Architecture

## Layers

1. DreamDEX SDK
2. Adapter
3. Market Service
4. Market Cache
5. Strategy
6. Risk Manager
7. Execution Manager

---

Strategy never talks directly to the exchange.

Execution Manager is the only component allowed to place orders.

Risk Manager approves or rejects every order before execution.