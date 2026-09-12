# Task

A team has a FastAPI application with `/health` and `/market-bars`. The latter
is meant to serve a Command Center frontend. Their release note says:

> Pydantic accepted one example, OpenAPI renders, `/health` returned 200, and a
> FastAPI release object was created. Therefore the frontend contract and live
> data path are verified.

Write a concise verification plan that corrects this conclusion. State the
authority that must be pinned for `/market-bars`, the local conformance
evidence, and the additional evidence needed after release. Explain what the
four current observations do and do not prove. This is a plan only; do not
claim new checks were executed.
