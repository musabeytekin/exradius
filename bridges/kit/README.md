# exradius-bridge-kit

A Python library for writing bridges between a work tracker (Jira, GitHub, ...) and an
[Exradius](https://github.com/musabeytekin/exradius) installation. A bridge implements the
`Tracker` interface and feeds the kit what its tracker's webhooks say; the kit owns the
flow: the task, the start comment naming the agent Exradius picked, questions and answers,
the result and the hand-back.

The design is in [docs/design.md](docs/design.md). `exradius_bridge_kit.testing` has the
fakes the kit is tested with, for a bridge's own tests.

```bash
uv sync
uv run pytest
EXRADIUS_KIT_TEST_BROKER_URL=amqp://guest:guest@localhost:5672/ uv run pytest -k real_broker
```

The broker test publishes to `exradius.events` and `exradius.answers`; use a throwaway
broker, or one whose Exradius can ignore an answer to an unknown request.
