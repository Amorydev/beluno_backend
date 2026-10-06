# ADR 0001: Modular Monolith

**Status:** Accepted

Beluno uses one Python codebase, one PostgreSQL authority, and bounded modules.
This preserves transactional correctness for money and sync while keeping
deployment and tracing manageable for a small team.

Microservices, Kafka, Redis, and multi-region writes require evidence from
measured constraints before adoption.
