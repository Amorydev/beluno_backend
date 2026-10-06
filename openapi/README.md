# OpenAPI Contract

FastAPI generates the source OpenAPI document from routers and Pydantic models.
`scripts/export_openapi.py` writes the reviewed snapshot to
`openapi/openapi.json`. CI regenerates it and fails if it is stale.

On pull requests CI also runs `scripts/check_openapi_compatibility.py` against
the base branch snapshot and fails on breaking changes (removed operations,
removed success-response properties, newly required inputs), then generates a
disposable typed client with `openapi-python-client` to prove the document is
consumable. Intentional breaking changes need a new API version path.
